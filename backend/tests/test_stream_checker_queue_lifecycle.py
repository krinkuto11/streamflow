#!/usr/bin/env python3
"""Regression tests for stream-checker queue clear/abort lifecycle."""

import os
import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
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

    def test_late_teamarr_priority_entry_does_not_reorder_active_channel(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(401, priority=10, stream_count=1))
        self.assertTrue(check_queue.add_channel(402, priority=10, stream_count=1))

        active_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(active_entry["channel_id"], 401)

        self.assertFalse(check_queue.add_channel(401, priority=100, stream_count=1))
        self.assertTrue(check_queue.add_channel(
            403,
            priority=100,
            stream_count=2,
            metadata={
                "source": "teamarr_preflight",
                "program_name": "Late Event Stream",
                "is_epg_scheduled": True,
                "forced_profile_id": "42",
            },
        ))

        status = check_queue.get_status()
        self.assertEqual(status["current_channel"], 401)
        self.assertEqual(status["in_progress"], 1)
        self.assertEqual(status["queued"], 2)

        teamarr_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(teamarr_entry["channel_id"], 403)
        self.assertEqual(teamarr_entry["metadata"]["source"], "teamarr_preflight")
        self.assertEqual(teamarr_entry["metadata"]["program_name"], "Late Event Stream")

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 402)

    def test_preflight_then_auto_create_then_normal_priority_order(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(401, priority=10, stream_count=1))
        self.assertTrue(check_queue.add_channel(402, priority=10, stream_count=1))

        active_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(active_entry["channel_id"], 401)

        self.assertTrue(check_queue.add_channel(
            403,
            priority=90,
            stream_count=1,
            metadata={
                "source": "auto_create",
                "program_name": "Live: MLB",
                "is_epg_scheduled": True,
            },
        ))
        self.assertTrue(check_queue.add_channel(
            404,
            priority=100,
            stream_count=1,
            metadata={
                "source": "teamarr_preflight",
                "program_name": "Home vs Away",
                "is_epg_scheduled": True,
            },
        ))

        preflight_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(preflight_entry["channel_id"], 404)
        self.assertEqual(preflight_entry["metadata"]["source"], "teamarr_preflight")

        auto_create_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(auto_create_entry["channel_id"], 403)
        self.assertEqual(auto_create_entry["metadata"]["source"], "auto_create")

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 402)

    def test_waiting_normal_channel_can_be_promoted_to_auto_create_priority(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(501, priority=10, stream_count=1))
        self.assertTrue(check_queue.add_channel(502, priority=10, stream_count=1))

        promoted = check_queue.add_channel(
            502,
            priority=90,
            stream_count=1,
            metadata={
                "source": "auto_create",
                "program_name": "Live: MLB",
                "is_epg_scheduled": True,
            },
        )

        self.assertTrue(promoted)
        status = check_queue.get_status()
        self.assertEqual(status["queued"], 2)
        self.assertEqual(status["queue_size"], 2)

        auto_create_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(auto_create_entry["channel_id"], 502)
        self.assertEqual(auto_create_entry["metadata"]["source"], "auto_create")

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 501)
        self.assertIsNone(check_queue.get_next_channel(timeout=0.1))
        self.assertEqual(check_queue.get_status()["queued"], 0)

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

    def test_queue_status_tracks_channel_duration_average(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(106, priority=10, stream_count=4))
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 106)
        with check_queue.lock:
            check_queue.channel_start_times[106] = datetime.now() - timedelta(seconds=20)

        self.assertTrue(check_queue.mark_completed(106))

        status = check_queue.get_status()
        self.assertGreaterEqual(status['avg_channel_process_time_sec'], 19)
        self.assertGreaterEqual(status['avg_stream_process_time_sec'], 4)

    def test_queue_eta_uses_channel_rate_as_conservative_floor(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 10 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 10} if key == 'concurrent_streams'
            else default
        )
        queue_status = {
            'completed': 28,
            'failed': 0,
            'queued': 184,
            'in_progress': 1,
            'total_queued': 212,
            'queued_streams_count': 3028,
            'in_progress_streams_count': 19,
            'avg_stream_process_time_sec': 6.31,
            'avg_channel_process_time_sec': 113.3,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertGreaterEqual(eta_seconds, int(113.3 * 185))
        self.assertEqual(queue_status['eta_basis'], 'channel')
        self.assertGreater(queue_status['eta_channel_seconds'], queue_status['eta_stream_seconds'])

    def test_queue_eta_uses_global_limit_for_stream_throughput(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 4 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 4} if key == 'concurrent_streams'
            else default
        )
        queue_status = {
            'completed': 20,
            'failed': 0,
            'queued': 5,
            'in_progress': 0,
            'total_queued': 25,
            'queued_streams_count': 100,
            'in_progress_streams_count': 0,
            'avg_stream_process_time_sec': 10,
            'avg_channel_process_time_sec': 1,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertEqual(queue_status['eta_stream_seconds'], 250)
        self.assertEqual(queue_status['eta_basis'], 'observed_stream')
        self.assertEqual(queue_status['eta_basis_detail'], 'observed_stream')
        self.assertEqual(eta_seconds, 250)

    def test_queue_eta_uses_configured_stream_duration_floor(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 4 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 4} if key == 'concurrent_streams'
            else {'ffmpeg_duration': 30} if key == 'stream_analysis'
            else default
        )
        queue_status = {
            'completed': 12,
            'failed': 0,
            'queued': 20,
            'in_progress': 0,
            'total_queued': 32,
            'queued_streams_count': 100,
            'in_progress_streams_count': 0,
            'avg_stream_process_time_sec': 6,
            'avg_channel_process_time_sec': 1,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertEqual(queue_status['eta_stream_seconds'], 750)
        self.assertEqual(queue_status['eta_channel_floor_seconds'], 1200)
        self.assertEqual(queue_status['eta_channel_seconds'], 1200)
        self.assertEqual(queue_status['eta_stream_observed_seconds'], 6)
        self.assertEqual(queue_status['eta_stream_floor_seconds'], 30)
        self.assertEqual(queue_status['eta_basis'], 'channel')
        self.assertEqual(queue_status['eta_basis_detail'], 'channel_floor')
        self.assertEqual(eta_seconds, 1200)

    def test_queue_eta_can_start_from_configured_stream_duration_before_samples(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 5 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 5} if key == 'concurrent_streams'
            else {'ffmpeg_duration': 30} if key == 'stream_analysis'
            else default
        )
        queue_status = {
            'completed': 0,
            'failed': 0,
            'queued': 10,
            'in_progress': 0,
            'total_queued': 10,
            'queued_streams_count': 50,
            'in_progress_streams_count': 0,
            'avg_stream_process_time_sec': 0,
            'avg_channel_process_time_sec': 0,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertEqual(queue_status['eta_stream_seconds'], 300)
        self.assertEqual(queue_status['eta_stream_floor_seconds'], 30)
        self.assertEqual(queue_status['eta_basis'], 'stream_floor')
        self.assertEqual(queue_status['eta_basis_detail'], 'stream_floor')
        self.assertEqual(eta_seconds, 300)

    def test_queue_eta_uses_timeout_and_channel_floor_for_large_full_run(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 10 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 10} if key == 'concurrent_streams'
            else {
                'ffmpeg_duration': 30,
                'timeout': 30,
                'stream_startup_buffer': 10,
            } if key == 'stream_analysis'
            else default
        )
        queue_status = {
            'completed': 23,
            'failed': 0,
            'queued': 188,
            'in_progress': 1,
            'total_queued': 212,
            'queued_streams_count': 3060,
            'in_progress_streams_count': 19,
            'avg_stream_process_time_sec': 6,
            'avg_channel_process_time_sec': 0,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertGreaterEqual(eta_seconds, 7 * 3600)
        self.assertEqual(queue_status['eta_stream_timeout_floor_seconds'], 70)
        self.assertEqual(queue_status['eta_channel_floor_seconds'], 26460)
        self.assertEqual(queue_status['eta_basis'], 'channel')
        self.assertEqual(queue_status['eta_basis_detail'], 'channel_floor')

    def test_queue_eta_uses_observed_workers_when_provider_waiting(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 10 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 10} if key == 'concurrent_streams'
            else {'ffmpeg_duration': 30} if key == 'stream_analysis'
            else default
        )
        queue_status = {
            'completed': 0,
            'failed': 0,
            'queued': 1,
            'in_progress': 0,
            'total_queued': 1,
            'queued_streams_count': 300,
            'in_progress_streams_count': 0,
            'avg_stream_process_time_sec': 0,
            'avg_channel_process_time_sec': 0,
            'eta_active_stream_workers': 3,
            'eta_provider_waiting_streams': 20,
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertEqual(queue_status['eta_configured_workers'], 10)
        self.assertEqual(queue_status['eta_effective_workers'], 3)
        self.assertEqual(queue_status['eta_stream_seconds'], 3000)
        self.assertEqual(eta_seconds, 3000)

    def test_queue_eta_uses_elapsed_channel_rate_when_channel_average_missing(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: (
            True if key == 'concurrent_streams.enabled'
            else 20 if key == 'concurrent_streams.global_limit'
            else {'global_limit': 20} if key == 'concurrent_streams'
            else default
        )
        queue_status = {
            'completed': 20,
            'failed': 0,
            'queued': 80,
            'in_progress': 0,
            'total_queued': 100,
            'queued_streams_count': 800,
            'in_progress_streams_count': 0,
            'avg_stream_process_time_sec': 1,
            'started_at': (datetime.now() - timedelta(hours=1)).isoformat(),
        }

        eta_seconds = service._calculate_queue_eta_seconds(queue_status)

        self.assertGreaterEqual(eta_seconds, 80 * 180)
        self.assertEqual(queue_status['eta_basis'], 'channel')
        self.assertGreater(queue_status['eta_channel_seconds'], queue_status['eta_stream_seconds'])

    def test_active_stream_reason_cleanup_removes_stale_wait_details(self):
        stream_status = {
            'id': 1,
            'status': 'waiting_provider_limit',
            'reason_detail': 'checking_capacity',
            'quality_reason': 'provider_capacity',
            'quality_reason_detail': 'checking_capacity',
            'quality_reason_context': {'profile_id': 10},
        }

        StreamCheckerService._clear_active_stream_reason(stream_status)

        self.assertNotIn('reason_detail', stream_status)
        self.assertNotIn('quality_reason', stream_status)
        self.assertNotIn('quality_reason_detail', stream_status)
        self.assertNotIn('quality_reason_context', stream_status)

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

    def test_worker_uses_single_channel_path_for_auto_create_queue_metadata(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        service.check_single_channel = Mock(return_value={'success': True})

        def pull_entry(timeout):
            service.running = False
            return {
                'channel_id': 9441,
                'metadata': {
                    'source': 'auto_create',
                    'program_name': 'Live: MLB',
                    'is_epg_scheduled': True,
                },
            }

        service.check_queue.get_next_entry.side_effect = pull_entry
        service.check_queue.mark_completed = Mock()
        service.check_queue.mark_failed = Mock()

        service._worker_loop()

        service._start_batch_changelog.assert_not_called()
        service._check_channel.assert_not_called()
        service.check_single_channel.assert_called_once_with(
            9441,
            program_name='Live: MLB',
            is_epg_scheduled=True,
            forced_profile_id=None,
        )
        service.check_queue.mark_completed.assert_called_once_with(9441)
        service.check_queue.mark_failed.assert_not_called()

    def test_worker_defers_specialized_queue_entries_while_direct_event_gate_is_active(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        service.check_single_channel = Mock()
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service.sync_batch_state = {"active": False}
        service._specialized_queue_gates = set()

        def stop_wait(timeout=None):
            service.running = False
            return None

        service.check_queue.get_next_entry.side_effect = stop_wait
        service.check_queue.defer_metadata_sources = Mock()

        service.set_specialized_queue_gate("teamarr_preflight_direct", True)
        service._worker_loop()

        service.check_queue.defer_metadata_sources.assert_any_call(
            {"teamarr_preflight", "auto_create"}
        )
        service.check_single_channel.assert_not_called()

    def test_deferred_specialized_queue_entry_waits_before_retry(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(
            8441,
            priority=100,
            stream_count=2,
            metadata={"source": "teamarr_preflight"},
        ))
        check_queue.defer_metadata_sources({"teamarr_preflight"})

        with patch("apps.stream.stream_checker_components.time.sleep") as sleep_mock:
            self.assertIsNone(check_queue.get_next_entry(timeout=0.2))

        sleep_mock.assert_called_once_with(0.2)
        status = check_queue.get_status()
        self.assertEqual(status["queued"], 1)
        self.assertEqual(status["in_progress"], 0)

    def test_worker_pauses_during_synchronous_batch(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        service.check_single_channel = Mock()
        service.lock = threading.Lock()
        service._sync_batch_generation = 3
        service.sync_batch_state = {"active": True, "generation": 3}
        sleep_calls = []

        def stop_after_sleep(_seconds):
            sleep_calls.append(_seconds)
            service.running = False

        with patch("apps.stream.stream_checker_service.time.sleep", side_effect=stop_after_sleep):
            service._worker_loop()

        service.check_queue.get_next_entry.assert_not_called()
        service._check_channel.assert_not_called()
        service.check_single_channel.assert_not_called()
        self.assertEqual(sleep_calls, [1])

    def test_worker_passes_teamarr_provider_limit_override_when_requested(self):
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
                    'provider_limit_override': True,
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

        service.check_single_channel.assert_called_once_with(
            8441,
            program_name='Home vs Away',
            is_epg_scheduled=True,
            forced_profile_id='42',
            provider_limit_override=True,
        )
        service.check_queue.mark_completed.assert_called_once_with(8441)

    def test_provider_limit_override_bypasses_capacity_and_active_viewers_are_stream_protected(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        streams = [{'id': 1, 'name': 'Event stream', 'm3u_account': 7}]
        udi = Mock()
        udi.is_channel_active.return_value = False
        udi.check_stream_can_run.return_value = (False, 'profile limit reached')

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi):
            self.assertIsNone(
                service._check_channel_limits(
                    77,
                    'Event Channel',
                    streams,
                    provider_limit_override=True,
                )
            )
            skipped = service._check_channel_limits(77, 'Event Channel', streams)

        self.assertEqual(skipped['skip_reason'], 'max_streams_reached')

        udi.is_channel_active.return_value = True
        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi):
            active_skip = service._check_channel_limits(
                77,
                'Event Channel',
                streams,
                provider_limit_override=True,
            )

        self.assertIsNone(active_skip)

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
            'good_streams_count': 4,
            'dead_streams_count': 3,
            'blank_streams_count': 1,
            'freeze_streams_count': 2,
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
        self.assertEqual(service.sync_batch_state['good_streams_count'], 0)
        self.assertEqual(service.sync_batch_state['dead_streams_count'], 0)
        self.assertEqual(service.sync_batch_state['blank_streams_count'], 0)
        self.assertEqual(service.sync_batch_state['freeze_streams_count'], 0)
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
            'good_streams_count': 4,
            'dead_streams_count': 3,
            'blank_streams_count': 1,
            'freeze_streams_count': 2,
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
        self.assertEqual(status['queue']['good_streams_count'], 4)
        self.assertEqual(status['queue']['dead_streams_count'], 3)
        self.assertEqual(status['queue']['blank_streams_count'], 1)
        self.assertEqual(status['queue']['freeze_streams_count'], 2)
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
            {
                'success': True,
                'channel_name': 'One',
                'good_streams_count': 4,
                'dead_streams_count': 2,
                'blank_streams_count': 1,
                'freeze_streams_count': 0,
            },
            {
                'success': True,
                'channel_name': 'Two',
                'good_streams_count': 3,
                'dead_streams_count': 1,
                'blank_streams_count': 0,
                'freeze_streams_count': 2,
            },
        ])

        udi = Mock()
        udi.get_channel_by_id.side_effect = lambda channel_id: {'streams': [{'id': f'{channel_id}-a'}]}
        progress_events = []
        sync_counts = []

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously(
                [101, 102],
                progress_callback=lambda completed, total, payload: progress_events.append(
                    (completed, total, payload.get('channel_name'))
                ) or sync_counts.append((
                    service.sync_batch_state.get('dead_streams_count'),
                    service.sync_batch_state.get('blank_streams_count'),
                    service.sync_batch_state.get('freeze_streams_count'),
                    service.sync_batch_state.get('good_streams_count'),
                )),
            )

        self.assertEqual(list(result.keys()), [101, 102])
        self.assertEqual(progress_events, [(1, 2, 'One'), (2, 2, 'Two')])
        self.assertEqual(sync_counts, [(2, 1, 0, 4), (3, 1, 2, 7)])
        self.assertFalse(service.sync_batch_state['active'])

    def test_sync_batch_counts_blank_and_freeze_from_checked_streams_fallback(self):
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
        service._check_channel_concurrent = Mock(return_value={
            'success': True,
            'channel_name': 'Fallback',
            'dead_streams_count': 2,
            'checked_streams': [
                {'id': 1, 'status': 'blank'},
                {'id': 2, 'status': 'completed', 'freeze_detected': True},
                {'id': 3, 'status': 'completed'},
            ],
        })

        udi = Mock()
        udi.get_channel_by_id.return_value = {
            'streams': [{'id': '101-a'}, {'id': '101-b'}, {'id': '101-c'}],
        }
        sync_counts = []

        with patch('apps.udi.get_udi_manager', return_value=udi):
            service.check_channels_synchronously(
                [101],
                progress_callback=lambda _completed, _total, _payload: sync_counts.append((
                    service.sync_batch_state.get('dead_streams_count'),
                    service.sync_batch_state.get('blank_streams_count'),
                    service.sync_batch_state.get('freeze_streams_count'),
                    service.sync_batch_state.get('good_streams_count'),
                )),
            )

        self.assertEqual(sync_counts, [(2, 1, 1, 1)])

    def test_sync_batch_drains_preflight_and_auto_create_serially(self):
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
        order = []
        service._check_channel_concurrent = Mock(side_effect=lambda channel_id, **_kwargs: (
            order.append(("batch", channel_id)) or {"success": True, "channel_name": f"Batch {channel_id}"}
        ))
        service.check_single_channel = Mock(side_effect=lambda channel_id, **_kwargs: (
            order.append(("special", channel_id, _kwargs.get("program_name"))) or {"success": True}
        ))

        self.assertTrue(service.check_queue.add_channel(
            501,
            priority=100,
            stream_count=1,
            metadata={
                "source": "teamarr_preflight",
                "program_name": "Teamarr Event",
                "is_epg_scheduled": True,
                "forced_profile_id": "42",
            },
        ))
        self.assertTrue(service.check_queue.add_channel(
            502,
            priority=90,
            stream_count=1,
            metadata={
                "source": "auto_create",
                "program_name": "Live: MLB",
                "is_epg_scheduled": True,
            },
        ))

        udi = Mock()
        udi.get_channel_by_id.side_effect = lambda channel_id: {'streams': [{'id': f'{channel_id}-a'}]}

        with patch('apps.udi.get_udi_manager', return_value=udi):
            service.check_channels_synchronously([101, 102])

        self.assertEqual(
            order,
            [
                ("special", 501, "Teamarr Event"),
                ("special", 502, "Live: MLB"),
                ("batch", 101),
                ("batch", 102),
            ],
        )
        self.assertEqual(service.check_queue.get_status()["queue_size"], 0)
        self.assertFalse(service.check_queue.get_status()["paused"])

    def test_sync_batch_isolates_teamarr_connectivity_abort_from_main_batch(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service._cancel_queueing = False
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: True if key == 'concurrent_streams.enabled' else default
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_concurrent = Mock(side_effect=lambda channel_id, **_kwargs: {
            "success": True,
            "channel_name": f"Batch {channel_id}",
        })

        def teamarr_connectivity_abort(channel_id, **_kwargs):
            service.abort_current_check.set()
            service._cancel_queueing = True
            return {
                "success": False,
                "aborted": True,
                "error": "connectivity_guard",
                "skip_reason": "connectivity_guard",
                "channel_id": channel_id,
            }

        service.check_single_channel = Mock(side_effect=teamarr_connectivity_abort)
        self.assertTrue(service.check_queue.add_channel(
            501,
            priority=100,
            stream_count=1,
            metadata={
                "source": "teamarr_preflight",
                "program_name": "Teamarr Event",
                "is_epg_scheduled": True,
                "forced_profile_id": "42",
            },
        ))

        udi = Mock()
        udi.get_channel_by_id.side_effect = lambda channel_id: {'streams': [{'id': f'{channel_id}-a'}]}
        teamarr_service = Mock()

        with patch('apps.udi.get_udi_manager', return_value=udi), patch(
            'apps.stream.teamarr_preflight_service.get_teamarr_preflight_service',
            return_value=teamarr_service,
        ):
            result = service.check_channels_synchronously([101, 102])

        self.assertEqual(list(result.keys()), [101, 102])
        self.assertEqual(service._check_channel_concurrent.call_count, 2)
        teamarr_service.record_queued_check_result.assert_called_once()
        self.assertEqual(service.check_queue.get_status()["queue_size"], 0)
        self.assertFalse(service.abort_current_check.is_set())
        self.assertFalse(service._cancel_queueing)

    def test_result_count_uses_checked_streams_when_summary_count_is_stale_zero(self):
        result = {
            'good_streams_count': 0,
            'blank_streams_count': 0,
            'freeze_streams_count': 0,
            'checked_streams': [
                {'id': 1, 'status': 'completed', 'blank_detected': True},
                {'id': 2, 'status': 'freeze', 'freeze_detected': False},
                {'id': 3, 'status': 'completed', 'freeze_detected': True},
            ],
        }

        self.assertEqual(
            StreamCheckerService._result_count(result, 'blank_streams_count', fallback_status='blank'),
            1,
        )
        self.assertEqual(
            StreamCheckerService._result_count(result, 'freeze_streams_count', fallback_status='freeze'),
            2,
        )
        self.assertEqual(StreamCheckerService._result_good_streams_count(result), 0)

    def test_result_good_count_accepts_legacy_clean_stream_stats_without_status(self):
        result = {
            'good_streams_count': 0,
            'checked_streams': [
                {'id': 1, 'resolution': '1920x1080', 'score': 95.0},
                {'id': 2, 'status': 'completed', 'score': 90.0},
                {'id': 3, 'status': 'revived', 'score': 88.0},
                {'id': 4, 'status': 'blank', 'blank_detected': True},
                {'id': 5, 'status': 'viewer_preempted'},
                {'id': 6, 'quality_reason_detail': 'error'},
                {'id': 7, 'status': 'completed', 'freeze_detected': True},
            ],
        }

        self.assertEqual(StreamCheckerService._result_good_streams_count(result), 3)

    def test_channel_reporting_preserves_dead_stream_cause_details(self):
        source_path = (
            Path(__file__).resolve().parents[1]
            / "apps"
            / "stream"
            / "stream_checker_service.py"
        )
        source = source_path.read_text(encoding="utf-8")

        self.assertIn("report_analyzed_streams = list(analyzed_streams)", source)
        self.assertIn("for analyzed in report_analyzed_streams:", source)

        result = {
            'dead_streams_count': 2,
            'checked_streams': [
                {
                    'stream_id': 'stream-blank',
                    'status': 'blank',
                    'blank_detected': True,
                    'freeze_detected': True,
                },
                {'stream_id': 'stream-unstable', 'status': 'dead'},
            ],
        }

        self.assertEqual(
            StreamCheckerService._result_count(result, 'blank_streams_count', fallback_status='blank'),
            1,
        )
        self.assertEqual(
            StreamCheckerService._result_count(result, 'freeze_streams_count', fallback_status='freeze'),
            1,
        )


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

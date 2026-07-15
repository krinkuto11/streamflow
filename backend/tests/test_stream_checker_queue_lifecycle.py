#!/usr/bin/env python3
"""Regression tests for stream-checker queue clear/abort lifecycle."""

import json
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
    add_to_stream_checker_queue_response,
    clear_stream_checker_queue_response,
    queue_all_channels_response,
)
from apps.stream.stream_checker_components import (  # noqa: E402
    ChannelUpdateTracker,
    StreamCheckQueue,
)
from apps.stream.stream_checker_service import StreamCheckerService  # noqa: E402


class TestStreamCheckQueueLifecycle(unittest.TestCase):
    @staticmethod
    def _in_memory_update_tracker():
        tracker = ChannelUpdateTracker.__new__(ChannelUpdateTracker)
        tracker.lock = threading.Lock()
        tracker.updates = {'channels': {}, 'last_global_check': None}
        tracker._save_updates = Mock()
        return tracker

    @staticmethod
    def _guard_snapshot(status):
        return {
            'entries_complete': status['entries_complete'],
            'admission_epoch': status['admission_epoch'],
            'admission_revision': status['admission_revision'],
            'paused': status['paused'],
            'queued_entries': [
                {
                    'channel_id': entry['channel_id'],
                    'entry_token': entry['entry_token'],
                    'metadata': dict(entry['metadata']),
                }
                for entry in status['queued_entries']
            ],
            'in_progress_entries': [
                {
                    'channel_id': entry['channel_id'],
                    'entry_token': entry['entry_token'],
                    'metadata': dict(entry['metadata']),
                }
                for entry in status['in_progress_entries']
            ],
            'completed_entries': [
                {
                    'channel_id': entry['channel_id'],
                    'entry_token': entry['entry_token'],
                    'metadata': dict(entry['metadata']),
                }
                for entry in status['completed_entries']
            ],
            'failed_entries': [
                {
                    'channel_id': entry['channel_id'],
                    'entry_token': entry['entry_token'],
                    'metadata': dict(entry['metadata']),
                }
                for entry in status['failed_entries']
            ],
            'completed_channel_ids': list(status['completed_channel_ids']),
            'failed_channel_ids': list(status['failed_channel_ids']),
        }

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

    def test_stale_promotion_entries_do_not_consume_logical_capacity(self):
        check_queue = StreamCheckQueue(max_size=2)

        self.assertTrue(check_queue.add_channel(601, priority=10))
        self.assertTrue(check_queue.add_channel(601, priority=20))
        self.assertEqual(check_queue.queue.qsize(), 2)
        self.assertEqual(check_queue.get_status()["queued"], 1)

        # The stale priority-10 heap item must not prevent the second distinct
        # channel from using the remaining logical queue slot.
        self.assertTrue(check_queue.add_channel(602, priority=15))
        self.assertTrue(check_queue.add_channel(602, priority=30))
        self.assertEqual(check_queue.queue.qsize(), 4)
        self.assertEqual(check_queue.get_status()["queued"], 2)

        # Promotions do not consume another slot, but a third distinct channel
        # still respects the configured logical maximum.
        self.assertFalse(check_queue.add_channel(603, priority=100))
        self.assertEqual(check_queue.get_status()["total_queued"], 2)

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 602)
        self.assertTrue(check_queue.mark_completed(602))
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 601)
        self.assertTrue(check_queue.mark_completed(601))
        self.assertIsNone(check_queue.get_next_channel(timeout=0.1))
        self.assertIsNone(check_queue.get_next_channel(timeout=0.1))
        self.assertEqual(check_queue.get_status()["queued"], 0)

    def test_concurrent_consumer_cannot_activate_superseded_promotion_entry(self):
        check_queue = StreamCheckQueue(max_size=2)
        current_popped = threading.Event()
        stale_popped = threading.Event()
        stale_finished = threading.Event()
        release_current = threading.Event()
        popped_items = {}
        results = {}
        errors = []
        original_get = check_queue.queue.get

        self.assertTrue(check_queue.add_channel(611, priority=10))
        self.assertTrue(check_queue.add_channel(611, priority=20))

        def controlled_get(*args, **kwargs):
            item = original_get(*args, **kwargs)
            popped_items[threading.current_thread().name] = item
            if item[0] == -20:
                current_popped.set()
                release_current.wait(timeout=10)
            else:
                stale_popped.set()
            return item

        def consume():
            name = threading.current_thread().name
            try:
                results[name] = check_queue.get_next_entry(timeout=1)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                item = popped_items.get(name)
                if item and item[0] == -10:
                    stale_finished.set()

        consumers = [
            threading.Thread(target=consume, name="queue-consumer-a"),
            threading.Thread(target=consume, name="queue-consumer-b"),
        ]
        with patch.object(check_queue.queue, "get", side_effect=controlled_get):
            for consumer in consumers:
                consumer.start()
            try:
                self.assertTrue(current_popped.wait(timeout=10))
                self.assertTrue(stale_popped.wait(timeout=10))
                self.assertTrue(stale_finished.wait(timeout=10))
            finally:
                release_current.set()
            for consumer in consumers:
                consumer.join(timeout=10)

        self.assertTrue(all(not consumer.is_alive() for consumer in consumers))
        self.assertEqual(errors, [])
        stale_consumer = next(
            name for name, item in popped_items.items() if item[0] == -10
        )
        current_consumer = next(
            name for name, item in popped_items.items() if item[0] == -20
        )
        self.assertIsNone(results[stale_consumer])
        self.assertEqual(results[current_consumer]["channel_id"], 611)
        self.assertEqual(check_queue.get_status()["in_progress"], 1)
        self.assertTrue(check_queue.mark_completed(611))

    def test_distinct_specialized_events_cannot_overwrite_one_queue_entry(self):
        check_queue = StreamCheckQueue(max_size=10)
        accepted = []

        self.assertTrue(check_queue.add_channel(
            77,
            priority=75,
            metadata={'source': 'teamarr_preflight', 'identity': 'event-a'},
            on_accepted=lambda: accepted.append('event-a'),
        ))
        self.assertFalse(check_queue.add_channel(
            77,
            priority=90,
            metadata={'source': 'teamarr_preflight', 'identity': 'event-b'},
            on_accepted=lambda: accepted.append('event-b'),
        ))

        self.assertEqual(accepted, ['event-a'])
        self.assertEqual(check_queue.queued_metadata[77]['identity'], 'event-a')
        self.assertEqual(check_queue.get_status()['queue_size'], 1)

    def test_force_generation_compare_clear_preserves_newer_request(self):
        tracker = self._in_memory_update_tracker()

        first_generation = tracker.mark_channel_for_force_check(77)
        second_generation = tracker.mark_channel_for_force_check(77)

        self.assertFalse(tracker.clear_force_check(
            77,
            expected_generation=first_generation,
        ))
        self.assertEqual(tracker.get_force_check_state(77), (True, second_generation))
        self.assertTrue(tracker.clear_force_check(
            77,
            expected_generation=second_generation,
        ))
        self.assertEqual(tracker.get_force_check_state(77), (False, second_generation))

    def test_specialized_drain_owns_and_clears_snapshotted_force_generation(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.abort_current_check = threading.Event()
        service.update_tracker = self._in_memory_update_tracker()
        service._run_specialized_queue_entry = Mock()

        self.assertTrue(service.check_queue.add_channel(
            77,
            priority=100,
            metadata={'source': 'teamarr_preflight'},
        ))
        generation = service.update_tracker.mark_channel_for_force_check(77)

        drained = service._drain_specialized_queue_entries(max_entries=1)

        self.assertEqual(drained, 1)
        queue_entry = service._run_specialized_queue_entry.call_args.args[0]
        self.assertEqual(queue_entry['channel_id'], 77)
        self.assertEqual(
            service._run_specialized_queue_entry.call_args.kwargs,
            {'force_check_generation': generation},
        )
        self.assertEqual(
            service.update_tracker.get_force_check_state(77),
            (False, generation),
        )

    def test_specialized_drain_migrates_legacy_force_and_preserves_requeue(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.abort_current_check = threading.Event()
        service.update_tracker = self._in_memory_update_tracker()
        service.update_tracker.updates['channels']['77'] = {'force_check': True}
        self.assertTrue(service.check_queue.add_channel(
            77,
            priority=100,
            metadata={'source': 'teamarr_preflight'},
        ))

        def requeue(_entry, *, force_check_generation):
            self.assertEqual(force_check_generation, 1)
            self.assertEqual(
                service.update_tracker.mark_channel_for_force_check(77),
                2,
            )

        service._run_specialized_queue_entry = Mock(side_effect=requeue)

        self.assertEqual(service._drain_specialized_queue_entries(max_entries=1), 1)
        self.assertEqual(
            service.update_tracker.get_force_check_state(77),
            (True, 2),
        )

    def test_profile_disabled_queue_entry_reaches_terminal_state(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.abort_current_check = threading.Event()
        service.checking = False
        service.config = Mock()
        service.config.get.side_effect = lambda _key, default=None: default
        service._build_threshold_config_from_profile = Mock(return_value={})
        automation_config = Mock()
        automation_config.get_effective_configuration.return_value = {
            'profile': {'stream_checking': {'enabled': False}},
        }
        udi = Mock()
        udi.get_channel_by_id.return_value = {
            'id': 77,
            'channel_group_id': 3,
        }
        self.assertTrue(service.check_queue.add_channel(77, priority=10))
        self.assertEqual(service.check_queue.get_next_channel(timeout=0.1), 77)

        with patch(
            'apps.stream.stream_checker_service.get_automation_config_manager',
            return_value=automation_config,
        ), patch(
            'apps.stream.stream_checker_service.get_udi_manager',
            return_value=udi,
        ):
            result = service._check_channel_concurrent(77)

        self.assertTrue(result['skipped'])
        self.assertEqual(result['skip_reason'], 'profile_disabled')
        self.assertFalse(service.checking)
        status = service.check_queue.get_status()
        self.assertEqual(status['in_progress'], 0)
        self.assertEqual(status['completed'], 1)

    def test_worker_finalizer_does_not_clear_newer_force_generation(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock(return_value=41)
        service._finalize_batch_changelog = Mock()
        service.update_tracker = self._in_memory_update_tracker()
        first_generation = service.update_tracker.mark_channel_for_force_check(77)

        service.check_queue.get_next_entry.return_value = {
            'channel_id': 77,
            'metadata': {},
            'queue_entry_token': 51,
        }

        def check_channel(channel_id, **kwargs):
            self.assertEqual(kwargs['force_check_generation'], first_generation)
            self.assertEqual(kwargs['batch_changelog_generation'], 41)
            self.assertEqual(kwargs['queue_entry_token'], 51)
            service.update_tracker.mark_channel_for_force_check(channel_id)
            service.running = False
            return {'skipped': True, 'skip_reason': 'active_viewers'}

        service._check_channel = check_channel

        service._worker_loop()

        pending, generation = service.update_tracker.get_force_check_state(77)
        self.assertTrue(pending)
        self.assertGreater(generation, first_generation)
        service._finalize_batch_changelog.assert_called_once_with(
            batch_generation=41,
        )

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

    def test_add_channels_is_atomic_when_worker_pops_first_heap_item(self):
        check_queue = StreamCheckQueue(max_size=10)
        first_item_popped = threading.Event()
        release_popped_item = threading.Event()
        accepted = []
        worker_result = {}
        original_put = check_queue.queue.put
        original_get = check_queue.queue.get

        def controlled_put(item, *args, **kwargs):
            result = original_put(item, *args, **kwargs)
            if check_queue._entry_channel_id(item) == 8726:
                self.assertTrue(first_item_popped.wait(timeout=10))
            return result

        def controlled_get(*args, **kwargs):
            item = original_get(*args, **kwargs)
            if check_queue._entry_channel_id(item) == 8726:
                first_item_popped.set()
                self.assertTrue(release_popped_item.wait(timeout=10))
            return item

        def consume_first_channel():
            worker_result['channel_id'] = check_queue.get_next_channel(timeout=10)

        worker = threading.Thread(target=consume_first_channel)
        with (
            patch.object(check_queue.queue, 'put', side_effect=controlled_put),
            patch.object(check_queue.queue, 'get', side_effect=controlled_get),
        ):
            worker.start()
            try:
                added = check_queue.add_channels(
                    [8726, 8725],
                    priority=10,
                    on_accepted=accepted.append,
                )
            finally:
                release_popped_item.set()

            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(added, 2)
        self.assertEqual(accepted, [8726, 8725])
        self.assertEqual(worker_result['channel_id'], 8726)

        active_status = check_queue.get_status()
        self.assertEqual(active_status['total_queued'], 2)
        self.assertEqual(active_status['in_progress'], 1)
        self.assertEqual(active_status['queued'], 1)

        self.assertTrue(check_queue.mark_completed(8726))
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 8725)
        self.assertTrue(check_queue.mark_completed(8725))

        completed_status = check_queue.get_status()
        self.assertEqual(completed_status['total_queued'], 2)
        self.assertEqual(completed_status['total_completed'], 2)

    def test_popped_paused_or_deferred_entry_is_restored_after_concurrent_add(self):
        for mode in ("paused", "deferred"):
            with self.subTest(mode=mode):
                check_queue = StreamCheckQueue(max_size=2)
                popped = threading.Event()
                release = threading.Event()
                worker_result = {}
                worker_errors = []
                original_get = check_queue.queue.get

                self.assertTrue(check_queue.add_channel(701, priority=10))
                self.assertTrue(check_queue.add_channel(
                    701,
                    priority=20,
                    metadata={"source": "sync_gate"},
                ))
                if mode == "paused":
                    check_queue.set_paused(True)
                else:
                    check_queue.defer_metadata_sources({"sync_gate"})

                def controlled_get(*args, **kwargs):
                    item = original_get(*args, **kwargs)
                    popped.set()
                    release.wait(timeout=10)
                    return item

                def pop_and_restore():
                    try:
                        worker_result["entry"] = check_queue.get_next_entry(timeout=0.05)
                    except BaseException as exc:  # pragma: no cover - asserted below
                        worker_errors.append(exc)

                worker = threading.Thread(target=pop_and_restore)
                with patch.object(check_queue.queue, "get", side_effect=controlled_get):
                    worker.start()
                    try:
                        self.assertTrue(popped.wait(timeout=10))
                        self.assertTrue(worker.is_alive())

                        # The worker has opened one physical heap slot without
                        # releasing its logical ownership. A concurrent producer
                        # fills the remaining logical slot and, in the formerly
                        # bounded heap, also filled the slot needed for restore.
                        self.assertTrue(check_queue.add_channel(702, priority=15))
                        self.assertEqual(check_queue.queue.qsize(), 2)
                    finally:
                        release.set()
                    worker.join(timeout=10)

                self.assertFalse(worker.is_alive())
                self.assertEqual(worker_errors, [])
                self.assertIsNone(worker_result["entry"])
                self.assertEqual(check_queue.queue.qsize(), 3)
                self.assertEqual(check_queue.get_status()["queued"], 2)

                if mode == "paused":
                    check_queue.set_paused(False)
                else:
                    check_queue.defer_metadata_sources(set())

                self.assertEqual(check_queue.get_next_channel(timeout=0.1), 701)
                self.assertTrue(check_queue.mark_completed(701))
                self.assertEqual(check_queue.get_next_channel(timeout=0.1), 702)
                self.assertTrue(check_queue.mark_completed(702))
                self.assertIsNone(check_queue.get_next_channel(timeout=0.1))
                self.assertEqual(check_queue.get_status()["queued"], 0)

    def test_promotion_flood_is_compacted_and_current_entry_restores_and_drains(self):
        check_queue = StreamCheckQueue(max_size=2)
        popped = threading.Event()
        release = threading.Event()
        worker_result = {}
        worker_errors = []

        self.assertTrue(check_queue.add_channel(801, priority=0))
        for priority in range(1, 501):
            self.assertTrue(check_queue.add_channel(801, priority=priority))

        with check_queue.lock:
            compaction_threshold = check_queue._heap_compaction_threshold_locked()
        self.assertLessEqual(check_queue.queue.qsize(), compaction_threshold)
        self.assertEqual(check_queue.get_status()["queued"], 1)

        check_queue.set_paused(True)
        original_get = check_queue.queue.get

        def controlled_get(*args, **kwargs):
            item = original_get(*args, **kwargs)
            popped.set()
            release.wait(timeout=10)
            return item

        def pop_and_restore():
            try:
                worker_result["entry"] = check_queue.get_next_entry(timeout=0.05)
            except BaseException as exc:  # pragma: no cover - asserted below
                worker_errors.append(exc)

        worker = threading.Thread(target=pop_and_restore)
        with patch.object(check_queue.queue, "get", side_effect=controlled_get):
            worker.start()
            try:
                self.assertTrue(popped.wait(timeout=10))
                self.assertTrue(check_queue.add_channel(802, priority=-1))
            finally:
                release.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_errors, [])
        self.assertIsNone(worker_result["entry"])
        self.assertEqual(check_queue.get_status()["queued"], 2)

        check_queue.set_paused(False)
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 801)
        self.assertTrue(check_queue.mark_completed(801))

        stale_entries_drained = 0
        second_channel = None
        while check_queue.queue.qsize():
            channel_id = check_queue.get_next_channel(timeout=0.1)
            if channel_id is None:
                stale_entries_drained += 1
                continue
            second_channel = channel_id
            self.assertTrue(check_queue.mark_completed(channel_id))

        self.assertGreater(stale_entries_drained, 0)
        self.assertEqual(second_channel, 802)
        self.assertEqual(check_queue.queue.qsize(), 0)
        completed_status = check_queue.get_status()
        self.assertEqual(completed_status["queued"], 0)
        self.assertEqual(completed_status["total_queued"], 2)
        self.assertEqual(completed_status["total_completed"], 2)

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
        self.assertEqual(queue_status['eta_stream_timeout_floor_seconds'], 90)
        self.assertEqual(queue_status['eta_channel_floor_seconds'], 34020)
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

    def test_disabled_concurrency_uses_smart_scheduler_with_one_probe_slot(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.config = Mock()
        service.config.get.side_effect = (
            lambda key, default=None: False
            if key == 'concurrent_streams.enabled'
            else default
        )
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_concurrent = Mock(return_value={'success': True})
        service._check_channel_sequential = Mock(side_effect=AssertionError(
            'legacy raw sequential checker must not be used'
        ))

        result = service._check_channel(
            404,
            run_mode='test',
            batch_changelog_generation=23,
            queue_entry_token=29,
        )

        self.assertTrue(result['success'])
        service._check_channel_concurrent.assert_called_once_with(
            404,
            skip_batch_changelog=False,
            forced_profile_id=None,
            provider_limit_override=False,
            run_mode='test',
            is_single_channel_check=False,
            global_limit_override=1,
            force_check_override=None,
            force_check_generation=None,
            batch_changelog_generation=23,
            queue_entry_token=29,
        )
        service._check_channel_sequential.assert_not_called()

    def test_status_reports_effective_single_worker_in_sequential_mode(self):
        from apps.api.stream_checker_handlers import get_stream_checker_status_response

        service = Mock()
        service.get_status.return_value = {}
        service.config.get.side_effect = lambda key, default=None: {
            'concurrent_streams.enabled': False,
            'concurrent_streams.global_limit': 10,
        }.get(key, default)
        app = Flask(__name__)

        with app.app_context():
            response = get_stream_checker_status_response(
                get_stream_checker_service=lambda: service,
                concurrent_streams_enabled_key='concurrent_streams.enabled',
                concurrent_streams_global_limit_key='concurrent_streams.global_limit',
            )

        self.assertEqual(response.get_json()['parallel'], {
            'enabled': False,
            'max_workers': 1,
            'configured_max_workers': 10,
            'mode': 'sequential',
        })

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

    def test_service_guarded_clear_succeeds_only_for_exact_snapshot(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = Mock()
        service._external_abort_generation = 4
        metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertTrue(service.check_queue.add_channel(
            105,
            metadata=metadata,
        ))
        expected = self._guard_snapshot(service.check_queue.get_status())

        result = service.clear_queue(expected_queue_snapshot=expected)

        self.assertTrue(result['guard_matched'])
        self.assertFalse(result['abort_requested'])
        self.assertEqual(result['cleared']['queued'], 1)
        self.assertIsNone(result['current'])
        self.assertEqual(service._external_abort_generation, 5)
        service.update_tracker.clear_force_checks.assert_called_once_with([105])
        service.progress.clear.assert_called_once()

    def test_service_guard_mismatch_has_no_abort_or_cleanup_side_effects(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.abort_current_check.set()
        service.progress = Mock()
        service.update_tracker = Mock()
        service._external_abort_generation = 7
        service._cancel_queueing = False
        service.batch_start_time = 'unchanged-batch'
        service.batch_changelog_entries = [{'channel_id': 105}]
        service._active_batch_changelog_generation = 12
        service._active_queue_entry_executions = {}
        self.assertTrue(service.check_queue.add_channel(
            105,
            metadata={
                'source': 'codex_bitrate_recheck',
                'run_label': 'bitrate-live-r9',
            },
        ))
        expected = self._guard_snapshot(service.check_queue.get_status())
        self.assertTrue(service.check_queue.add_channel(
            999,
            metadata={'source': 'foreign_run'},
        ))
        before = self._guard_snapshot(service.check_queue.get_status())

        result = service.clear_queue(expected_queue_snapshot=expected)

        self.assertFalse(result['guard_matched'])
        self.assertFalse(result['abort_requested'])
        self.assertIsNone(result['cleared'])
        self.assertEqual(service._external_abort_generation, 7)
        self.assertTrue(service.abort_current_check.is_set())
        self.assertFalse(service._cancel_queueing)
        self.assertEqual(service.batch_start_time, 'unchanged-batch')
        self.assertEqual(service.batch_changelog_entries, [{'channel_id': 105}])
        self.assertEqual(service._active_batch_changelog_generation, 12)
        self.assertEqual(
            self._guard_snapshot(service.check_queue.get_status()),
            before,
        )
        service.update_tracker.clear_force_checks.assert_not_called()
        service.progress.clear.assert_not_called()

    def test_guarded_clear_rejects_pause_change_without_revision_mutation(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(105))
        expected = self._guard_snapshot(check_queue.get_status())

        # Direct/sync checker paths historically update paused while already
        # holding the queue lock, without going through set_paused(). Model
        # that exact path so the atomic guard cannot rely on revision alone.
        with check_queue.lock:
            check_queue.paused = True
        self.assertEqual(
            check_queue.get_status()['admission_revision'],
            expected['admission_revision'],
        )

        result = check_queue.clear_if_entries_match(
            expected_admission_epoch=expected['admission_epoch'],
            expected_admission_revision=expected['admission_revision'],
            expected_queued_entries=expected['queued_entries'],
            expected_in_progress_entries=expected['in_progress_entries'],
            expected_completed_entries=expected['completed_entries'],
            expected_failed_entries=expected['failed_entries'],
            expected_completed_channel_ids=expected['completed_channel_ids'],
            expected_failed_channel_ids=expected['failed_channel_ids'],
            expected_paused=expected['paused'],
        )

        self.assertFalse(result['guard_matched'])
        self.assertTrue(result['current']['paused'])
        status = check_queue.get_status()
        self.assertTrue(status['paused'])
        self.assertEqual(status['queued'], 1)
        self.assertIsNone(status['last_cleared_at'])

    def test_service_guarded_clear_preserves_matching_paused_state(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = Mock()
        service._external_abort_generation = 2
        self.assertTrue(service.check_queue.add_channel(105))
        with service.check_queue.lock:
            service.check_queue.paused = True
        expected = self._guard_snapshot(service.check_queue.get_status())

        result = service.clear_queue(expected_queue_snapshot=expected)

        self.assertTrue(result['guard_matched'])
        self.assertTrue(service.check_queue.get_status()['paused'])
        self.assertEqual(service.check_queue.get_status()['queued'], 0)

    def test_service_guard_rejects_unrepresented_direct_or_tombstone_owner(self):
        for owner_state in ('direct', 'tombstone'):
            with self.subTest(owner_state=owner_state):
                service = StreamCheckerService.__new__(StreamCheckerService)
                service.check_queue = StreamCheckQueue(max_size=10)
                service.lock = threading.Lock()
                service.sync_batch_state = {'active': False}
                service.checking = False
                service.abort_current_check = threading.Event()
                service.progress = Mock()
                service._external_abort_generation = 9
                service._single_stream_check_active = owner_state == 'direct'
                service._active_queue_entry_executions = (
                    {(999, 41): {'cancelled': True}}
                    if owner_state == 'tombstone'
                    else {}
                )
                self.assertTrue(service.check_queue.add_channel(105))
                expected = self._guard_snapshot(service.check_queue.get_status())

                result = service.clear_queue(expected_queue_snapshot=expected)

                self.assertFalse(result['guard_matched'])
                self.assertEqual(
                    result['guard_failure_reason'],
                    'active_owner_not_in_snapshot',
                )
                self.assertIsNone(result['cleared'])
                self.assertEqual(service._external_abort_generation, 9)
                self.assertEqual(service.check_queue.get_status()['queued'], 1)
                service.progress.clear.assert_not_called()

    def test_irreversible_channel_side_effect_linearizes_with_request_abort(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.lock = threading.Lock()
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._external_abort_generation = 3
        service._cancel_queueing = False
        service._active_queue_entry_executions = {
            (105, 11): {'cancelled': False},
        }
        entered_action = threading.Event()
        release_action = threading.Event()
        abort_started = threading.Event()
        events = []
        commit_result = {}

        def irreversible_action():
            entered_action.set()
            release_action.wait(timeout=10)
            events.append('write_finished')
            return 'written'

        commit_thread = threading.Thread(
            target=lambda: commit_result.setdefault(
                'value',
                service._run_channel_side_effect_if_authorized(
                    105,
                    11,
                    irreversible_action,
                ),
            ),
        )

        def abort_request():
            abort_started.set()
            service.request_abort(reason='race_test')
            events.append('abort_returned')

        abort_thread = threading.Thread(target=abort_request)
        commit_thread.start()
        self.assertTrue(entered_action.wait(timeout=10))
        abort_thread.start()
        self.assertTrue(abort_started.wait(timeout=10))
        self.assertTrue(abort_thread.is_alive())

        release_action.set()
        commit_thread.join(timeout=10)
        abort_thread.join(timeout=10)

        self.assertFalse(commit_thread.is_alive())
        self.assertFalse(abort_thread.is_alive())
        self.assertEqual(commit_result['value'], (True, 'written'))
        self.assertEqual(events, ['write_finished', 'abort_returned'])
        self.assertTrue(service.abort_current_check.is_set())
        self.assertTrue(
            service._active_queue_entry_executions[(105, 11)]['cancelled']
        )

        late_action = Mock()
        self.assertEqual(
            service._run_channel_side_effect_if_authorized(
                105,
                11,
                late_action,
            ),
            (False, None),
        )
        late_action.assert_not_called()

    def test_no_stream_visibility_paths_require_exact_queue_authorization(self):
        automation_config = Mock()
        automation_config.get_effective_configuration.return_value = {}
        udi = Mock()
        udi.get_channel_by_id.return_value = {
            'id': 105,
            'name': 'No Streams',
            'channel_group_id': 7,
        }

        for method_name in ('_check_channel_concurrent', '_check_channel_sequential'):
            with self.subTest(method_name=method_name):
                service = StreamCheckerService.__new__(StreamCheckerService)
                service.config = Mock()
                service.config.get.side_effect = (
                    lambda _key, default=None: default
                )
                service.progress = Mock()
                service.changelog = None
                service.check_queue = Mock()
                service.abort_current_check = threading.Event()
                service._abort_channel_check_if_requested = Mock(return_value=None)
                service._run_channel_side_effect_if_authorized = Mock(
                    return_value=(False, None)
                )
                service._apply_channel_visibility_after_check = Mock()
                service._abort_channel_check = Mock(return_value={
                    'success': False,
                    'aborted': True,
                    'channel_id': 105,
                })

                with patch(
                    'apps.stream.stream_checker_service.get_automation_config_manager',
                    return_value=automation_config,
                ), patch(
                    'apps.stream.stream_checker_service.get_udi_manager',
                    return_value=udi,
                ), patch(
                    'apps.stream.stream_checker_service.fetch_channel_streams',
                    return_value=[],
                ), patch(
                    'apps.stream.stream_checker_service._get_base_url',
                    return_value='http://localhost:9191',
                ):
                    result = getattr(service, method_name)(
                        105,
                        queue_entry_token=11,
                    )

                self.assertTrue(result['aborted'])
                service._run_channel_side_effect_if_authorized.assert_called_once()
                self.assertEqual(
                    service._run_channel_side_effect_if_authorized.call_args.args[:2],
                    (105, 11),
                )
                service._apply_channel_visibility_after_check.assert_not_called()
                service._abort_channel_check.assert_called_once_with(
                    105,
                    'No Streams',
                    queue_entry_token=11,
                )

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

    def test_active_clear_discards_batch_and_late_add_cannot_leak_into_next_batch(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.batch_lock = threading.Lock()
        service.batch_start_time = None
        service.batch_changelog_entries = []
        service._batch_changelog_generation = 0
        service._active_batch_changelog_generation = None
        service.changelog = Mock()
        service.sync_batch_state = {'active': False}
        service.checking = True
        service.abort_current_check = threading.Event()
        service.progress = Mock()

        first_generation = service._start_batch_changelog()
        self.assertEqual(first_generation, 1)
        self.assertTrue(service._add_to_batch_changelog(
            {'channel_id': 101},
            batch_generation=first_generation,
        ))

        self.assertTrue(service.check_queue.add_channel(101, priority=10))
        self.assertEqual(
            service.check_queue.get_next_entry(timeout=0.1)['channel_id'],
            101,
        )

        result = service.clear_queue()

        self.assertTrue(result['abort_requested'])
        self.assertIsNone(service.batch_start_time)
        self.assertEqual(service.batch_changelog_entries, [])
        self.assertIsNone(service._active_batch_changelog_generation)

        # The cleared worker may still finish its current call. Its late result
        # must not resurrect the old batch or become part of the next one.
        self.assertFalse(service._add_to_batch_changelog(
            {'channel_id': 101, 'late': True},
            batch_generation=first_generation,
        ))
        self.assertIsNone(service.batch_start_time)
        self.assertEqual(service.batch_changelog_entries, [])

        service.checking = False
        service.abort_current_check.clear()
        second_generation = service._start_batch_changelog(
            require_not_aborted=True,
        )
        fresh_start_time = service.batch_start_time
        self.assertEqual(second_generation, 2)
        self.assertTrue(service._add_to_batch_changelog(
            {'channel_id': 202},
            batch_generation=second_generation,
        ))

        self.assertIsNotNone(fresh_start_time)
        self.assertEqual(service.batch_changelog_entries, [{'channel_id': 202}])

        self.assertFalse(service._add_to_batch_changelog(
            {'channel_id': 101, 'late_again': True},
            batch_generation=first_generation,
        ))
        self.assertFalse(service._finalize_batch_changelog(
            batch_generation=first_generation,
        ))
        self.assertEqual(service.batch_start_time, fresh_start_time)
        self.assertEqual(service.batch_changelog_entries, [{'channel_id': 202}])
        service.changelog.add_entry.assert_not_called()

    def test_idle_clear_finalizes_completed_batch_and_forces_fresh_claim(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.batch_lock = threading.Lock()
        service.batch_start_time = '2000-01-01T00:00:00'
        completed_entry = {'channel_id': 101, 'success': True}
        service.batch_changelog_entries = [completed_entry]
        service._batch_changelog_generation = 1
        service._active_batch_changelog_generation = 1
        service.changelog = Mock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()

        result = service.clear_queue()

        self.assertFalse(result['abort_requested'])
        self.assertTrue(result['batch_changelog_finalized'])
        self.assertTrue(result['batch_changelog_detached'])
        service.changelog.add_entry.assert_called_once()
        self.assertEqual(
            service.changelog.add_entry.call_args.kwargs['timestamp'],
            '2000-01-01T00:00:00',
        )
        self.assertIsNone(service.batch_start_time)
        self.assertEqual(service.batch_changelog_entries, [])
        self.assertIsNone(service._active_batch_changelog_generation)

        next_generation = service._start_batch_changelog(
            require_not_aborted=True,
        )
        self.assertEqual(next_generation, 2)
        self.assertNotEqual(service.batch_start_time, '2000-01-01T00:00:00')
        self.assertEqual(service.batch_changelog_entries, [])

    def test_queue_entry_token_rejects_clear_requeue_same_channel_aba(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(105, priority=10))
        old_entry = check_queue.get_next_entry(timeout=0.1)

        self.assertTrue(check_queue.owns_in_progress(
            105,
            old_entry['queue_entry_token'],
        ))
        check_queue.clear(reason='aba_test')
        self.assertTrue(check_queue.add_channel(105, priority=10))
        new_entry = check_queue.get_next_entry(timeout=0.1)

        self.assertNotEqual(
            old_entry['queue_entry_token'],
            new_entry['queue_entry_token'],
        )
        self.assertFalse(check_queue.owns_in_progress(
            105,
            old_entry['queue_entry_token'],
        ))
        self.assertFalse(check_queue.mark_failed(
            105,
            'stale owner',
            entry_token=old_entry['queue_entry_token'],
        ))
        self.assertTrue(check_queue.owns_in_progress(
            105,
            new_entry['queue_entry_token'],
        ))
        self.assertTrue(check_queue.mark_completed(
            105,
            entry_token=new_entry['queue_entry_token'],
        ))

    def test_queue_status_exposes_complete_tokenized_entry_snapshot(self):
        check_queue = StreamCheckQueue(max_size=10)
        metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertEqual(
            check_queue.add_channels(
                [105, 106],
                priority=90,
                metadata=metadata,
            ),
            2,
        )

        queued_status = check_queue.get_status()
        self.assertTrue(queued_status['entries_complete'])
        self.assertEqual(
            [entry['channel_id'] for entry in queued_status['queued_entries']],
            [105, 106],
        )
        self.assertEqual(queued_status['in_progress_entries'], [])
        for entry in queued_status['queued_entries']:
            self.assertEqual(entry['metadata'], metadata)
            self.assertIsInstance(entry['entry_token'], int)

        active_entry = check_queue.get_next_entry(timeout=0.1)
        active_status = check_queue.get_status()
        self.assertEqual(
            active_status['in_progress_entries'],
            [{
                'channel_id': 105,
                'stream_count': 1,
                'metadata': metadata,
                'entry_token': active_entry['queue_entry_token'],
            }],
        )
        self.assertEqual(
            [entry['channel_id'] for entry in active_status['queued_entries']],
            [106],
        )

    def test_queue_admission_deep_copies_nested_metadata_provenance(self):
        check_queue = StreamCheckQueue(max_size=10)
        metadata = {
            'source': 'internal_event',
            'match_evidence': {'teams': ['Home', 'Away']},
        }
        self.assertTrue(check_queue.add_channel(
            105,
            metadata=metadata,
        ))
        admitted = check_queue.get_status()
        admitted_revision = admitted['admission_revision']

        metadata['match_evidence']['teams'][0] = 'Mutated'

        current = check_queue.get_status()
        self.assertEqual(current['admission_revision'], admitted_revision)
        self.assertEqual(
            current['queued_entries'][0]['metadata']['match_evidence'],
            {'teams': ['Home', 'Away']},
        )

    def test_exact_snapshot_guard_atomically_clears_full_lifecycle(self):
        check_queue = StreamCheckQueue(max_size=10)
        metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertEqual(check_queue.add_channels(
            [105, 106],
            priority=90,
            metadata=metadata,
        ), 2)
        active_entry = check_queue.get_next_entry(timeout=0.1)
        self.assertTrue(check_queue.mark_completed(
            active_entry['channel_id'],
            entry_token=active_entry['queue_entry_token'],
        ))
        expected = self._guard_snapshot(check_queue.get_status())
        self.assertEqual(expected['completed_entries'], [{
            'channel_id': active_entry['channel_id'],
            'entry_token': active_entry['queue_entry_token'],
            'metadata': metadata,
        }])

        result = check_queue.clear_if_entries_match(
            expected_admission_epoch=expected['admission_epoch'],
            expected_admission_revision=expected['admission_revision'],
            expected_queued_entries=expected['queued_entries'],
            expected_in_progress_entries=expected['in_progress_entries'],
            expected_completed_entries=expected['completed_entries'],
            expected_failed_entries=expected['failed_entries'],
            expected_completed_channel_ids=expected['completed_channel_ids'],
            expected_failed_channel_ids=expected['failed_channel_ids'],
            expected_paused=expected['paused'],
            reason='guarded_test',
        )

        self.assertTrue(result['guard_matched'])
        self.assertEqual(result['cleared']['queued'], 1)
        self.assertEqual(result['cleared']['completed'], 1)
        self.assertIsNone(result['current'])
        cleared_status = check_queue.get_status()
        self.assertEqual(cleared_status['state'], 'cleared')
        self.assertGreater(
            cleared_status['admission_revision'],
            expected['admission_revision'],
        )

    def test_guard_rejects_new_foreign_entry_without_mutation(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(
            105,
            metadata={
                'source': 'codex_bitrate_recheck',
                'run_label': 'bitrate-live-r9',
            },
        ))
        expected = self._guard_snapshot(check_queue.get_status())
        self.assertTrue(check_queue.add_channel(
            999,
            metadata={'source': 'foreign_run', 'run_label': 'foreign-r1'},
        ))
        before_clear = check_queue.get_status()

        result = check_queue.clear_if_entries_match(
            expected_admission_epoch=expected['admission_epoch'],
            expected_admission_revision=expected['admission_revision'],
            expected_queued_entries=expected['queued_entries'],
            expected_in_progress_entries=expected['in_progress_entries'],
            expected_completed_entries=expected['completed_entries'],
            expected_failed_entries=expected['failed_entries'],
            expected_completed_channel_ids=expected['completed_channel_ids'],
            expected_failed_channel_ids=expected['failed_channel_ids'],
            expected_paused=expected['paused'],
        )

        self.assertFalse(result['guard_matched'])
        self.assertIsNone(result['cleared'])
        after_clear = check_queue.get_status()
        self.assertEqual(
            self._guard_snapshot(after_clear),
            self._guard_snapshot(before_clear),
        )
        self.assertIsNone(after_clear['last_cleared_at'])

    def test_guard_revision_rejects_accepted_duplicate_force_intent(self):
        check_queue = StreamCheckQueue(max_size=10)
        metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertTrue(check_queue.add_channel(
            105,
            priority=90,
            metadata=metadata,
        ))
        expected = self._guard_snapshot(check_queue.get_status())
        accepted_force_intent = Mock()

        self.assertTrue(check_queue.add_channel(
            105,
            priority=80,
            on_accepted=accepted_force_intent,
        ))
        current = check_queue.get_status()
        self.assertEqual(
            current['queued_entries'][0]['entry_token'],
            expected['queued_entries'][0]['entry_token'],
        )
        self.assertEqual(current['queued_entries'][0]['metadata'], metadata)
        self.assertGreater(
            current['admission_revision'],
            expected['admission_revision'],
        )
        accepted_force_intent.assert_called_once()

        result = check_queue.clear_if_entries_match(
            expected_admission_epoch=expected['admission_epoch'],
            expected_admission_revision=expected['admission_revision'],
            expected_queued_entries=expected['queued_entries'],
            expected_in_progress_entries=expected['in_progress_entries'],
            expected_completed_entries=expected['completed_entries'],
            expected_failed_entries=expected['failed_entries'],
            expected_completed_channel_ids=expected['completed_channel_ids'],
            expected_failed_channel_ids=expected['failed_channel_ids'],
            expected_paused=expected['paused'],
        )

        self.assertFalse(result['guard_matched'])
        self.assertEqual(check_queue.get_status()['queued'], 1)

    def test_guard_rejects_foreign_terminal_entry_and_prior_process_epoch(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(105))
        expected = self._guard_snapshot(check_queue.get_status())
        active = check_queue.get_next_entry(timeout=0.1)
        self.assertTrue(check_queue.mark_failed(
            105,
            'foreign terminal result',
            entry_token=active['queue_entry_token'],
        ))

        terminal_result = check_queue.clear_if_entries_match(
            expected_admission_epoch=expected['admission_epoch'],
            expected_admission_revision=expected['admission_revision'],
            expected_queued_entries=expected['queued_entries'],
            expected_in_progress_entries=expected['in_progress_entries'],
            expected_completed_entries=expected['completed_entries'],
            expected_failed_entries=expected['failed_entries'],
            expected_completed_channel_ids=expected['completed_channel_ids'],
            expected_failed_channel_ids=expected['failed_channel_ids'],
            expected_paused=expected['paused'],
        )
        self.assertFalse(terminal_result['guard_matched'])
        failed_status = check_queue.get_status()
        self.assertEqual(failed_status['failed_channel_ids'], [105])
        self.assertEqual(failed_status['failed_entries'], [{
            'channel_id': 105,
            'entry_token': active['queue_entry_token'],
            'metadata': {},
        }])

        current = self._guard_snapshot(check_queue.get_status())
        restarted_queue = StreamCheckQueue(max_size=10)
        self.assertNotEqual(
            restarted_queue.get_status()['admission_epoch'],
            current['admission_epoch'],
        )
        epoch_result = check_queue.clear_if_entries_match(
            expected_admission_epoch=restarted_queue.get_status()[
                'admission_epoch'
            ],
            expected_admission_revision=current['admission_revision'],
            expected_queued_entries=current['queued_entries'],
            expected_in_progress_entries=current['in_progress_entries'],
            expected_completed_entries=current['completed_entries'],
            expected_failed_entries=current['failed_entries'],
            expected_completed_channel_ids=current['completed_channel_ids'],
            expected_failed_channel_ids=current['failed_channel_ids'],
            expected_paused=current['paused'],
        )
        self.assertFalse(epoch_result['guard_matched'])
        self.assertEqual(check_queue.get_status()['failed_channel_ids'], [105])

    def test_terminal_identity_cannot_be_reused_by_foreign_same_channel_run(self):
        check_queue = StreamCheckQueue(max_size=10)
        own_metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertTrue(check_queue.add_channel(
            105,
            metadata=own_metadata,
            immutable_metadata_keys=set(own_metadata),
        ))
        own_active = check_queue.get_next_entry(timeout=0.1)
        self.assertTrue(check_queue.mark_completed(
            105,
            entry_token=own_active['queue_entry_token'],
        ))
        own_terminal = self._guard_snapshot(check_queue.get_status())

        self.assertTrue(check_queue.remove_from_completed(105))
        foreign_metadata = {
            'source': 'foreign_run',
            'run_label': 'foreign-r1',
        }
        self.assertTrue(check_queue.add_channel(
            105,
            metadata=foreign_metadata,
            immutable_metadata_keys=set(foreign_metadata),
        ))
        foreign_active = check_queue.get_next_entry(timeout=0.1)
        self.assertTrue(check_queue.mark_completed(
            105,
            entry_token=foreign_active['queue_entry_token'],
        ))
        foreign_terminal = self._guard_snapshot(check_queue.get_status())

        self.assertNotEqual(
            foreign_terminal['completed_entries'],
            own_terminal['completed_entries'],
        )
        self.assertEqual(foreign_terminal['completed_entries'], [{
            'channel_id': 105,
            'entry_token': foreign_active['queue_entry_token'],
            'metadata': foreign_metadata,
        }])
        self.assertNotEqual(
            foreign_active['queue_entry_token'],
            own_active['queue_entry_token'],
        )

        stale_owner_clear = check_queue.clear_if_entries_match(
            expected_admission_epoch=own_terminal['admission_epoch'],
            expected_admission_revision=own_terminal['admission_revision'],
            expected_queued_entries=own_terminal['queued_entries'],
            expected_in_progress_entries=own_terminal['in_progress_entries'],
            expected_completed_entries=own_terminal['completed_entries'],
            expected_failed_entries=own_terminal['failed_entries'],
            expected_completed_channel_ids=own_terminal[
                'completed_channel_ids'
            ],
            expected_failed_channel_ids=own_terminal['failed_channel_ids'],
            expected_paused=own_terminal['paused'],
        )
        self.assertFalse(stale_owner_clear['guard_matched'])
        self.assertEqual(
            check_queue.get_status()['completed_entries'],
            foreign_terminal['completed_entries'],
        )

    def test_forced_duplicate_cannot_steal_queue_admission_provenance(self):
        check_queue = StreamCheckQueue(max_size=10)
        foreign_metadata = {
            'source': 'other_run',
            'run_label': 'foreign-r1',
        }
        own_metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertTrue(check_queue.add_channel(
            105,
            priority=100,
            metadata=foreign_metadata,
            immutable_metadata_keys=set(foreign_metadata),
        ))
        original_entry = check_queue.get_status()['queued_entries'][0]
        force_callback = Mock()

        self.assertFalse(check_queue.add_channel(
            105,
            priority=90,
            metadata=own_metadata,
            immutable_metadata_keys=set(own_metadata),
            on_accepted=force_callback,
        ))
        coalesced_entry = check_queue.get_status()['queued_entries'][0]
        self.assertEqual(coalesced_entry['entry_token'], original_entry['entry_token'])
        self.assertEqual(coalesced_entry['metadata'], foreign_metadata)

        self.assertFalse(check_queue.add_channel(
            105,
            priority=110,
            metadata=own_metadata,
            immutable_metadata_keys=set(own_metadata),
            on_accepted=force_callback,
        ))
        promoted_entry = check_queue.get_status()['queued_entries'][0]
        self.assertEqual(promoted_entry['entry_token'], original_entry['entry_token'])
        self.assertEqual(promoted_entry['metadata'], foreign_metadata)
        force_callback.assert_not_called()

        same_owner_callback = Mock()
        self.assertTrue(check_queue.add_channel(
            105,
            priority=100,
            metadata=foreign_metadata,
            immutable_metadata_keys=set(foreign_metadata),
            on_accepted=same_owner_callback,
        ))
        same_owner_callback.assert_called_once()

    def test_protected_owner_rejects_partial_owner_and_auto_create_adoption(self):
        check_queue = StreamCheckQueue(max_size=10)
        protected_metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }
        self.assertTrue(check_queue.add_channel(
            105,
            priority=90,
            metadata=protected_metadata,
            immutable_metadata_keys=set(protected_metadata),
        ))
        baseline = check_queue.get_status()

        partial_callback = Mock()
        self.assertFalse(check_queue.add_channel(
            105,
            priority=90,
            metadata={'source': protected_metadata['source']},
            immutable_metadata_keys={'source'},
            on_accepted=partial_callback,
        ))
        partial_callback.assert_not_called()

        auto_create_callback = Mock()
        self.assertFalse(check_queue.add_channel(
            105,
            priority=110,
            metadata={
                'source': 'auto_create',
                'program_name': 'Protected Event',
                'is_epg_scheduled': True,
            },
            on_accepted=auto_create_callback,
        ))
        auto_create_callback.assert_not_called()

        current = check_queue.get_status()
        self.assertEqual(
            current['admission_revision'],
            baseline['admission_revision'],
        )
        self.assertEqual(
            current['queued_entries'],
            baseline['queued_entries'],
        )

    def test_cancelled_queue_execution_fences_new_owners_until_worker_ack(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service._single_channel_check_active = False
        service._automation_cycle_active = False
        service._sync_batch_execution_active = False
        service._active_queue_entry_executions = {}
        service._external_abort_generation = 0
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.progress.get.return_value = {
            'channel_id': 105,
            'status': 'starting',
        }
        service.batch_lock = threading.Lock()
        service.batch_start_time = None
        service.batch_changelog_entries = []
        service._active_batch_changelog_generation = None
        service._batch_changelog_generation = 0

        self.assertTrue(service.check_queue.add_channel(105, priority=10))
        old_entry = service.check_queue.get_next_entry(timeout=0.1)
        old_token = old_entry['queue_entry_token']
        self.assertTrue(service._claim_queue_entry_execution(105, old_token))

        clear_result = service.clear_queue()

        self.assertTrue(clear_result['abort_requested'])
        self.assertTrue(service.abort_current_check.is_set())
        self.assertTrue(
            service._active_queue_entry_executions[(105, old_token)]['cancelled']
        )
        self.assertFalse(service._begin_single_channel_check_operation())
        self.assertFalse(service._begin_single_stream_check_operation())
        self.assertFalse(service.begin_automation_cycle_operation())

        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}
        with patch('apps.udi.get_udi_manager', return_value=udi):
            sync_result = service.check_channels_synchronously([105])
        self.assertEqual(sync_result[105]['error'], 'stream_checker_active')

        # Even an accidental shared-event clear cannot revive a cancelled owner.
        service.abort_current_check.clear()
        abort_result = service._abort_channel_check_if_requested(
            105,
            'Old Queue Owner',
            queue_entry_token=old_token,
        )
        self.assertTrue(abort_result['aborted'])
        service.progress.clear_if_matches.assert_called_with({
            'channel_id': 105,
            'status': 'starting',
        })

        service._release_queue_entry_execution(105, old_token)
        self.assertEqual(service._active_queue_entry_executions, {})
        self.assertTrue(service._begin_single_channel_check_operation())
        service._end_single_channel_check_operation()

    def test_queue_execution_release_is_token_exact_and_stale_cleanup_is_inert(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.lock = threading.Lock()
        service._active_queue_entry_executions = {
            (105, 11): {'cancelled': False},
            (105, 12): {'cancelled': False},
        }
        service.progress = Mock()
        service.progress.get.return_value = {
            'channel_id': 105,
            'status': 'new-owner-progress',
        }

        service._release_queue_entry_execution(105, 11)

        self.assertNotIn((105, 11), service._active_queue_entry_executions)
        self.assertIn((105, 12), service._active_queue_entry_executions)
        self.assertFalse(service._clear_queue_entry_progress(105, 11))
        service.progress.clear_if_matches.assert_not_called()

    def test_specialized_queue_execution_releases_reservation_on_exception(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.lock = threading.Lock()
        service.check_queue = Mock()
        service.check_queue.owns_in_progress.return_value = True
        service._active_queue_entry_executions = {}
        service.progress = Mock()
        service._run_specialized_queue_entry_owned = Mock(
            side_effect=RuntimeError('specialized failure')
        )
        entry = {
            'channel_id': 105,
            'queue_entry_token': 81,
            'metadata': {'source': 'teamarr_preflight'},
        }

        with self.assertRaisesRegex(RuntimeError, 'specialized failure'):
            service._run_specialized_queue_entry(entry)

        self.assertEqual(service._active_queue_entry_executions, {})

    def test_request_abort_wins_before_normal_queue_completion(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._active_queue_entry_executions = {}
        service._external_abort_generation = 0
        service._cancel_queueing = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.progress.get.return_value = None
        on_completed = Mock()

        self.assertTrue(service.check_queue.add_channel(105, priority=10))
        entry = service.check_queue.get_next_entry(timeout=0.1)
        token = entry['queue_entry_token']
        self.assertTrue(service._claim_queue_entry_execution(105, token))

        service.request_abort('completion-linearization-test')
        accepted = service._complete_channel_check(
            105,
            on_completed,
            queue_entry_token=token,
        )

        self.assertFalse(accepted)
        on_completed.assert_not_called()
        status = service.check_queue.get_status()
        self.assertEqual(status['completed'], 0)
        self.assertEqual(status['failed'], 1)
        self.assertEqual(status['total_completed'], 0)
        self.assertEqual(status['total_failed'], 1)
        service._release_queue_entry_execution(105, token)

    def test_request_abort_wins_before_specialized_success_terminalization(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._active_queue_entry_executions = {}
        service._external_abort_generation = 0
        service._cancel_queueing = False
        service._sync_batch_execution_active = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.progress.get.return_value = None

        def complete_after_abort(*_args, **_kwargs):
            service.request_abort('specialized-linearization-test')
            return {'success': True}

        service.check_single_channel = Mock(side_effect=complete_after_abort)
        teamarr_service = Mock()
        self.assertTrue(service.check_queue.add_channel(
            105,
            priority=100,
            metadata={'source': 'teamarr_preflight'},
        ))
        entry = service.check_queue.get_next_entry(timeout=0.1)

        with patch(
            'apps.stream.teamarr_preflight_service.get_teamarr_preflight_service',
            return_value=teamarr_service,
        ):
            service._run_specialized_queue_entry(entry)

        status = service.check_queue.get_status()
        self.assertEqual(status['completed'], 0)
        self.assertEqual(status['failed'], 1)
        self.assertEqual(status['total_completed'], 0)
        self.assertEqual(status['total_failed'], 1)
        self.assertEqual(service._active_queue_entry_executions, {})
        teamarr_service.record_queued_check_result.assert_not_called()

    def test_specialized_outer_callback_uses_exact_execution_after_same_channel_requeue(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._active_queue_entry_executions = {}
        service.abort_current_check = threading.Event()
        callback = Mock()

        self.assertTrue(service.check_queue.add_channel(105, priority=100))
        old_entry = service.check_queue.get_next_entry(timeout=0.1)
        old_token = old_entry['queue_entry_token']
        self.assertTrue(service._claim_queue_entry_execution(105, old_token))
        self.assertTrue(service._complete_channel_check(
            105,
            queue_entry_token=old_token,
        ))

        # A producer can requeue the channel after the nested quality path made
        # the visible queue terminal but before the specialized wrapper returns.
        service.check_queue.remove_from_completed(105)
        self.assertTrue(service.check_queue.add_channel(105, priority=100))
        new_token = service.check_queue.get_status()['queued_entries'][0]['entry_token']
        self.assertNotEqual(old_token, new_token)

        service._complete_channel_check(
            105,
            callback,
            queue_entry_token=old_token,
            allow_already_completed_side_effects=True,
        )

        callback.assert_called_once_with()
        queued_status = service.check_queue.get_status()
        self.assertEqual(queued_status['queued'], 1)
        self.assertEqual(queued_status['queued_entries'][0]['entry_token'], new_token)
        service._release_queue_entry_execution(105, old_token)

    def test_clear_wins_before_specialized_failure_callback(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._active_queue_entry_executions = {}
        service._external_abort_generation = 0
        service._cancel_queueing = False
        service._sync_batch_execution_active = False
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.progress.get.return_value = None
        service.batch_lock = threading.Lock()
        service.batch_start_time = None
        service.batch_changelog_entries = []
        service._active_batch_changelog_generation = None
        service._batch_changelog_generation = 0
        teamarr_service = Mock()

        def fail_after_clear(*_args, **_kwargs):
            service.clear_queue()
            return {'success': False, 'error': 'probe_failure'}

        service.check_single_channel = Mock(side_effect=fail_after_clear)
        self.assertTrue(service.check_queue.add_channel(
            105,
            priority=100,
            metadata={'source': 'teamarr_preflight'},
        ))
        entry = service.check_queue.get_next_entry(timeout=0.1)

        with patch(
            'apps.stream.teamarr_preflight_service.get_teamarr_preflight_service',
            return_value=teamarr_service,
        ):
            service._run_specialized_queue_entry(entry)

        status = service.check_queue.get_status()
        self.assertEqual(status['state'], 'cleared')
        self.assertEqual(status['completed'], 0)
        self.assertEqual(status['failed'], 0)
        self.assertTrue(service.abort_current_check.is_set())
        self.assertEqual(service._active_queue_entry_executions, {})
        teamarr_service.record_queued_check_result.assert_not_called()

    def test_clear_queue_cancels_queue_owned_force_marker(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = self._in_memory_update_tracker()
        service.update_tracker.mark_channel_for_force_check(77)
        self.assertTrue(service.check_queue.add_channel(77, priority=10))

        service.clear_queue()

        self.assertEqual(
            service.update_tracker.get_force_check_state(77)[0],
            False,
        )

    def test_automation_reservation_blocks_direct_operations_and_allows_owner_sync(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service._single_channel_check_active = False
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service._sync_batch_generation = 0
        service._external_abort_generation = 0
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.return_value = True
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_concurrent = Mock(return_value={'success': True})
        service._apply_specialized_queue_deferral = Mock()
        service._drain_specialized_queue_entries = Mock(return_value=0)
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        self.assertTrue(service.begin_automation_cycle_operation())
        self.assertFalse(service._begin_single_channel_check_operation())
        self.assertFalse(service._begin_single_stream_check_operation())

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously([77], force_check=True)

        self.assertTrue(result[77]['success'])
        self.assertTrue(service._automation_cycle_active)
        self.assertTrue(service.end_automation_cycle_operation())
        self.assertFalse(service._automation_cycle_active)
        self.assertFalse(service.check_queue.paused)

    def test_automation_owner_abort_is_not_cleared_by_nested_sync_batch(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service._single_channel_check_active = False
        service._sync_batch_execution_active = False
        service._sync_batch_generation = 0
        service._external_abort_generation = 0
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service._check_channel_concurrent = Mock()
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        self.assertTrue(service.begin_automation_cycle_operation())
        service.request_abort('test_manual_stop')

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously([77], force_check=True)

        self.assertTrue(result[77]['aborted'])
        self.assertEqual(result[77]['error'], 'aborted')
        self.assertTrue(service.abort_current_check.is_set())
        service._check_channel_concurrent.assert_not_called()
        self.assertTrue(service.end_automation_cycle_operation())

    def test_status_progress_cleanup_excludes_new_direct_reservation(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service._single_channel_check_active = False
        service._sync_batch_execution_active = False
        service.abort_current_check = threading.Event()
        clear_started = threading.Event()
        release_clear = threading.Event()
        reservation_done = threading.Event()
        service.progress = Mock()

        def blocking_clear(expected):
            self.assertEqual(expected, {'channel_id': 1})
            clear_started.set()
            self.assertTrue(release_clear.wait(2))
            return True

        service.progress.clear_if_matches.side_effect = blocking_clear
        cleanup_thread = threading.Thread(
            target=service._clear_progress_snapshot_if_idle,
            args=({'channel_id': 1},),
        )
        cleanup_thread.start()
        self.assertTrue(clear_started.wait(2))

        def reserve_direct():
            service._begin_single_channel_check_operation()
            reservation_done.set()

        reservation_thread = threading.Thread(target=reserve_direct)
        reservation_thread.start()
        self.assertFalse(reservation_done.wait(0.1))

        release_clear.set()
        cleanup_thread.join(2)
        reservation_thread.join(2)
        self.assertTrue(reservation_done.is_set())
        self.assertTrue(service._single_channel_check_active)
        service._end_single_channel_check_operation()

    def test_worker_pop_claim_clear_race_fails_batch_claim_closed(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.batch_changelog_entries = []
        service.batch_lock = threading.Lock()
        service._batch_changelog_generation = 0
        service._active_batch_changelog_generation = None
        service.changelog = Mock()
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service._sync_batch_generation = 0
        service._specialized_queue_gates = set()
        service._external_abort_generation = 0
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.check_queue = StreamCheckQueue(max_size=10)
        service._check_channel = Mock()
        self.assertTrue(service.check_queue.add_channel(105, priority=10))

        popped = threading.Event()
        original_get_next_entry = service.check_queue.get_next_entry
        pull_count = 0

        def pull_entry(timeout):
            nonlocal pull_count
            pull_count += 1
            if pull_count == 1:
                entry = original_get_next_entry(timeout=timeout)
                popped.set()
                return entry
            service.running = False
            return None

        service.check_queue.get_next_entry = Mock(side_effect=pull_entry)
        clear_result = {}
        service.batch_lock.acquire()
        batch_lock_released = False
        worker_thread = threading.Thread(target=service._worker_loop)
        clear_thread = threading.Thread(
            target=lambda: clear_result.setdefault('value', service.clear_queue()),
        )
        try:
            worker_thread.start()
            self.assertTrue(popped.wait(2))
            self.assertEqual(service.check_queue.get_status()['in_progress'], 1)

            clear_thread.start()
            self.assertTrue(service.abort_current_check.wait(2))

            # Both the worker claim and clear are waiting for batch_lock now.
            # Since clear published abort first, the worker must fail closed
            # regardless of which waiter acquires the lock next.
            service.batch_lock.release()
            batch_lock_released = True
        finally:
            if not batch_lock_released and service.batch_lock.locked():
                service.batch_lock.release()

        clear_thread.join(5)
        worker_thread.join(5)

        self.assertFalse(clear_thread.is_alive())
        self.assertFalse(worker_thread.is_alive())
        self.assertTrue(clear_result['value']['abort_requested'])
        service._check_channel.assert_not_called()
        self.assertEqual(service._batch_changelog_generation, 0)
        self.assertIsNone(service._active_batch_changelog_generation)
        self.assertIsNone(service.batch_start_time)
        self.assertEqual(service.batch_changelog_entries, [])

    def test_worker_preserves_external_abort_for_owned_popped_entry(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.check_queue = Mock()
        service.check_queue.owns_in_progress.return_value = True
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        seen_abort_states = []

        def pull_entry(timeout):
            service.abort_current_check.set()
            return {
                'channel_id': 105,
                'metadata': {},
                'queue_entry_token': 73,
            }

        def check_channel(channel_id, **kwargs):
            seen_abort_states.append(service.abort_current_check.is_set())
            service.running = False
            return service._abort_channel_check(
                channel_id,
                queue_entry_token=kwargs['queue_entry_token'],
            )

        service.check_queue.get_next_entry.side_effect = pull_entry
        service._check_channel = check_channel

        service._worker_loop()

        self.assertEqual(seen_abort_states, [True])
        service.check_queue.mark_failed.assert_called_once_with(
            105,
            'aborted',
            entry_token=73,
        )
        service._start_batch_changelog.assert_not_called()

    def test_worker_uses_single_channel_path_for_teamarr_queue_metadata(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.lock = threading.Lock()
        service._external_abort_generation = 0
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
                'queue_entry_token': 81,
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
            run_mode='teamarr_preflight',
            _operation_already_reserved=True,
            _queue_force_check_generation=None,
            _queue_entry_token=81,
        )
        teamarr_service.record_queued_check_result.assert_called_once()
        queued_metadata, result = teamarr_service.record_queued_check_result.call_args.args
        self.assertEqual(queued_metadata['source'], 'teamarr_preflight')
        self.assertEqual(result, {'success': True})
        service.check_queue.mark_completed.assert_called_once_with(
            8441,
            entry_token=81,
        )
        service.check_queue.mark_failed.assert_not_called()

    def test_worker_uses_single_channel_path_for_auto_create_queue_metadata(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.lock = threading.Lock()
        service._external_abort_generation = 0
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
                'queue_entry_token': 91,
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
            _operation_already_reserved=True,
            _queue_force_check_generation=None,
            _queue_entry_token=91,
        )
        service.check_queue.mark_completed.assert_called_once_with(
            9441,
            entry_token=91,
        )
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

    def test_worker_preserves_abort_while_direct_stream_check_is_active(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.abort_current_check.set()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        service.lock = threading.Lock()
        service._single_stream_check_active = True
        service._sync_batch_generation = 0
        service.sync_batch_state = {"active": False, "generation": 0}

        def stop_after_sleep(_seconds):
            service.running = False

        with patch("apps.stream.stream_checker_service.time.sleep", side_effect=stop_after_sleep):
            service._worker_loop()

        self.assertTrue(service.abort_current_check.is_set())
        service.check_queue.get_next_entry.assert_not_called()
        service._check_channel.assert_not_called()

    def test_worker_passes_teamarr_provider_limit_override_when_requested(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.lock = threading.Lock()
        service._external_abort_generation = 0
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
                'queue_entry_token': 101,
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
            run_mode='teamarr_preflight',
            _operation_already_reserved=True,
            _queue_force_check_generation=None,
            _queue_entry_token=101,
        )
        service.check_queue.mark_completed.assert_called_once_with(
            8441,
            entry_token=101,
        )

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

    def test_account_without_profiles_reaches_atomic_account_fallback(self):
        from apps.udi.manager import UDIManager

        service = StreamCheckerService.__new__(StreamCheckerService)
        stream = {
            'id': 1,
            'name': 'Fallback stream',
            'url': 'http://provider.invalid/live/1',
            'm3u_account_id': 7,
        }
        udi = UDIManager()
        udi._m3u_accounts_cache = [{
            'id': 7,
            'name': 'Provider fallback',
            'max_streams': 1,
            'profiles': [],
        }]
        udi._streams_cache = [stream]
        udi._initialized = True
        udi._build_indexes()

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=udi):
            result = service._check_channel_limits(77, 'Fallback Channel', [stream])

        self.assertIsNone(result)

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
            'channels_hidden': 1,
            'channels_ready': 1,
            'channel_visibility_changed': 2,
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
        self.assertEqual(service.sync_batch_state['channels_hidden'], 0)
        self.assertEqual(service.sync_batch_state['channels_ready'], 0)
        self.assertEqual(service.sync_batch_state['channel_visibility_changed'], 0)
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
            'channels_hidden': 1,
            'channels_ready': 1,
            'channel_visibility_changed': 2,
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
        self.assertEqual(status['queue']['channels_hidden'], 1)
        self.assertEqual(status['queue']['channels_ready'], 1)
        self.assertEqual(status['queue']['channel_visibility_changed'], 2)
        self.assertEqual(status['queue']['started_at'], '2026-05-29T18:03:41')
        self.assertFalse(status['queue']['entries_complete'])
        self.assertEqual(
            status['queue']['entries_unavailable_reason'],
            'sync_batch_active',
        )
        self.assertTrue(status['stream_checking_mode'])

    def _service_for_idle_progress_status(self, progress_payload):
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
        service.progress.get.return_value = progress_payload
        service.update_tracker = Mock()
        service.update_tracker.get_last_global_check.return_value = None
        return service

    def test_get_status_keeps_sync_execution_active_after_visible_state_clears(self):
        service = self._service_for_idle_progress_status(None)
        service._sync_batch_execution_active = True

        status = service.get_status()

        self.assertTrue(status['sync_batch_execution_active'])
        self.assertTrue(status['stream_checking_mode'])
        self.assertFalse(status['checking'])
        self.assertFalse(status['queue']['queue_size'])

    def test_get_status_keeps_cancelled_queue_execution_non_idle_until_ack(self):
        service = self._service_for_idle_progress_status(None)
        service._active_queue_entry_executions = {
            (105, 81): {'cancelled': True},
        }

        status = service.get_status()

        self.assertTrue(status['queue_execution_active'])
        self.assertTrue(status['checking'])
        self.assertTrue(status['stream_checking_mode'])
        self.assertFalse(status['queue']['queue_size'])
        self.assertFalse(status['queue']['in_progress'])

    def test_get_status_marks_stale_batch_progress_when_no_check_is_active(self):
        service = self._service_for_idle_progress_status({
            'status': 'analyzing',
            'channel_id': 1946,
            'timestamp': datetime.now().isoformat(),
            'streams_detail': [{'id': 1, 'status': 'checking'}],
        })

        status = service.get_status()

        self.assertFalse(status['stream_checking_mode'])
        self.assertTrue(status['progress_stale'])
        self.assertEqual(status['progress_stale_details']['reason'], 'idle_batch_progress')
        self.assertTrue(status['progress']['stale'])
        self.assertEqual(status['progress']['stale_reason'], 'idle_batch_progress')
        service.progress.clear.assert_not_called()

    def test_get_status_does_not_report_stopped_waiting_queue_as_active_check(self):
        service = self._service_for_idle_progress_status(None)
        service.running = False
        service.check_queue.add_channel(
            9234,
            priority=100,
            stream_count=3,
            metadata={'source': 'teamarr_preflight'},
        )

        status = service.get_status()

        self.assertEqual(status['queue']['state'], 'queued')
        self.assertEqual(status['queue']['queue_size'], 1)
        self.assertFalse(status['running'])
        self.assertFalse(status['checking'])
        self.assertFalse(status['stream_checking_mode'])

    def test_get_status_keeps_recent_single_channel_progress_active_when_mode_lags(self):
        service = self._service_for_idle_progress_status({
            'status': 'preparing',
            'channel_id': 1946,
            'is_single_channel_check': True,
            'timestamp': (datetime.now() - timedelta(seconds=60)).isoformat(),
            'streams_detail': [{'id': 1, 'status': 'pending'}],
        })

        status = service.get_status()

        self.assertTrue(status['stream_checking_mode'])
        self.assertFalse(status['progress_stale'])
        self.assertFalse(status['progress_stale_details'])
        self.assertFalse(status['progress'].get('stale', False))
        service.progress.clear.assert_not_called()

    def test_get_status_marks_old_single_channel_progress_stale(self):
        service = self._service_for_idle_progress_status({
            'status': 'checking',
            'channel_id': 1946,
            'is_single_channel_check': True,
            'timestamp': (datetime.now() - timedelta(minutes=20)).isoformat(),
            'streams_detail': [{'id': 1, 'status': 'checking'}],
        })

        status = service.get_status()

        self.assertFalse(status['stream_checking_mode'])
        self.assertTrue(status['progress_stale'])
        self.assertEqual(status['progress_stale_details']['reason'], 'no_active_worker')
        self.assertTrue(status['progress']['stale'])
        self.assertEqual(status['progress']['stale_reason'], 'no_active_worker')
        self.assertGreater(status['progress']['stale_age_seconds'], status['progress']['stale_after_seconds'])
        service.progress.clear.assert_not_called()

    def test_get_status_reports_read_only_dispatcharr_status_stale_risk(self):
        service = self._service_for_idle_progress_status(None)

        class FakeUdi:
            def is_network_ready(self):
                return True

            def is_automation_busy(self):
                return False

            def get_observability_status(self):
                return {
                    'network_ready': True,
                    'init_in_progress': False,
                    'refresh_running': False,
                    'last_refresh_time': '2026-06-13T12:07:27',
                    'last_refresh_age_seconds': 60,
                }

            def get_m3u_accounts(self):
                return [
                    {
                        'id': 5,
                        'name': 'Provider A',
                        'is_active': True,
                        'status': 'fetching',
                        'last_message': 'Processing completed in 168.5 seconds. Streams: 0 created, 10 updated.',
                        'updated_at': '2026-06-13T08:02:50Z',
                    },
                    {
                        'id': 7,
                        'name': 'Provider B',
                        'is_active': True,
                        'status': 'success',
                        'last_message': 'Processing completed in 140.9 seconds.',
                        'updated_at': '2026-06-13T10:02:28Z',
                    },
                ]

        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=FakeUdi()):
            status = service.get_status()

        diagnostics = status['external_stale_diagnostics']
        self.assertEqual(diagnostics['status'], 'stale_risk')
        self.assertTrue(diagnostics['read_only'])
        self.assertTrue(diagnostics['stale_status_suspected'])
        self.assertFalse(diagnostics['actions']['dispatcharr_mutated'])
        self.assertFalse(diagnostics['actions']['dispatcharr_restart_attempted'])
        self.assertTrue(diagnostics['actions']['repair_requires_operator_approval'])
        self.assertEqual(diagnostics['m3u_accounts']['status_counts'], {'fetching': 1, 'success': 1})
        self.assertEqual(diagnostics['m3u_accounts']['stale_suspected_count'], 1)
        self.assertEqual(
            diagnostics['m3u_accounts']['stale_suspected'][0]['conflict'],
            'active_status_with_completed_message',
        )
        self.assertEqual(diagnostics['external_checks']['celery']['status'], 'unknown')
        self.assertEqual(diagnostics['external_checks']['redis']['status'], 'unknown')
        self.assertEqual(diagnostics['external_checks']['postgres']['status'], 'unknown')

    def test_external_stale_diagnostics_waits_for_udi_network_refresh(self):
        service = self._service_for_idle_progress_status(None)

        class FakeUdi:
            def __init__(self):
                self.accounts_called = False

            def is_network_ready(self):
                return False

            def is_automation_busy(self):
                return False

            def get_observability_status(self):
                return {
                    'network_ready': False,
                    'init_in_progress': True,
                    'refresh_running': False,
                }

            def get_m3u_accounts(self):
                self.accounts_called = True
                return []

        fake_udi = FakeUdi()
        with patch('apps.stream.stream_checker_service.get_udi_manager', return_value=fake_udi):
            status = service.get_status()

        diagnostics = status['external_stale_diagnostics']
        self.assertEqual(diagnostics['status'], 'insufficient_evidence')
        self.assertFalse(diagnostics['stale_status_suspected'])
        self.assertFalse(fake_udi.accounts_called)

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
                'channel_visibility': {
                    'action': 'hidden',
                    'changed': True,
                },
            },
            {
                'success': True,
                'channel_name': 'Two',
                'good_streams_count': 3,
                'dead_streams_count': 1,
                'blank_streams_count': 0,
                'freeze_streams_count': 2,
                'channel_visibility': {
                    'action': 'unhidden',
                    'changed': True,
                },
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
                    service.sync_batch_state.get('channels_hidden'),
                    service.sync_batch_state.get('channels_ready'),
                    service.sync_batch_state.get('channel_visibility_changed'),
                )),
            )

        self.assertEqual(list(result.keys()), [101, 102])
        self.assertEqual(progress_events, [(1, 2, 'One'), (2, 2, 'Two')])
        self.assertEqual(sync_counts, [(2, 1, 0, 4, 1, 0, 1), (3, 1, 2, 7, 1, 1, 2)])
        self.assertFalse(service.sync_batch_state['active'])

    def test_sync_batch_rejects_active_direct_stream_reservation(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service.sync_batch_state = {'active': False}
        service.checking = True
        service._single_stream_check_active = True
        service.abort_current_check = threading.Event()
        service.update_tracker = Mock()
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_concurrent = Mock()
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously([101], force_check=True)

        self.assertEqual(result[101]['error'], 'stream_checker_active')
        self.assertTrue(result[101]['aborted'])
        self.assertTrue(service._single_stream_check_active)
        self.assertFalse(service._sync_batch_execution_active)
        service.update_tracker.mark_channel_for_force_check.assert_not_called()
        service._check_channel_concurrent.assert_not_called()

    def test_sync_batch_check_failure_releases_execution_and_restores_queue_pause(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service.abort_current_check = threading.Event()
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.return_value = True
        service._check_channel_concurrent = Mock(
            side_effect=RuntimeError('quality check failed')
        )
        service._require_quality_check_connectivity = Mock(return_value=None)
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously([101], force_check=True)

        self.assertEqual(result[101]['error'], 'quality check failed')
        service._check_channel_concurrent.assert_called_once_with(
            101,
            skip_batch_changelog=True,
            target_stream_ids=None,
            run_mode=None,
            force_check_override=True,
        )
        service.update_tracker.mark_channel_for_force_check.assert_not_called()
        service.update_tracker.clear_force_check.assert_not_called()
        self.assertFalse(service._sync_batch_execution_active)
        self.assertIsNone(service._sync_batch_execution_generation)
        self.assertFalse(service.sync_batch_state['active'])
        self.assertFalse(service.check_queue.paused)
        self.assertFalse(service.checking)

    def test_sync_batch_releases_checking_with_queued_handoff_before_worker_pickup(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service._single_channel_check_active = False
        service._automation_cycle_active = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = self._in_memory_update_tracker()
        service.config = Mock()
        service.config.get.side_effect = (
            lambda key, default=None: True
            if key == 'concurrent_streams.enabled'
            else default
        )
        service._require_quality_check_connectivity = Mock(return_value=None)

        def queue_handoff(_channel_id, **_kwargs):
            self.assertTrue(service.check_queue.add_channel(202, priority=5))
            return {'success': True}

        service._check_channel_concurrent = Mock(side_effect=queue_handoff)
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously([101])

        self.assertTrue(result[101]['success'])
        self.assertEqual(service.check_queue.get_status()['queued'], 1)
        self.assertFalse(service.checking)

        service.clear_queue()
        self.assertFalse(service.abort_current_check.is_set())
        self.assertTrue(service._begin_single_channel_check_operation())
        service._end_single_channel_check_operation()

    def test_worker_preserves_clear_abort_until_sync_execution_finishes(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service._sync_batch_execution_active = False
        service._sync_batch_execution_generation = None
        service.sync_batch_state = {'active': False}
        service.checking = False
        service._single_stream_check_active = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.side_effect = (
            lambda key, default=None: True
            if key == 'concurrent_streams.enabled'
            else default
        )
        service._require_quality_check_connectivity = Mock(return_value=None)
        check_started = threading.Event()
        release_check = threading.Event()
        thread_result = {}

        def blocking_check(channel_id, **_kwargs):
            check_started.set()
            self.assertTrue(release_check.wait(5))
            return {
                'success': False,
                'aborted': service.abort_current_check.is_set(),
                'channel_id': channel_id,
            }

        service._check_channel_concurrent = Mock(side_effect=blocking_check)
        udi = Mock()
        udi.get_channel_by_id.return_value = {'streams': [{'id': 1}]}

        with patch('apps.udi.get_udi_manager', return_value=udi):
            sync_thread = threading.Thread(
                target=lambda: thread_result.setdefault(
                    'result',
                    service.check_channels_synchronously([101]),
                )
            )
            sync_thread.start()
            self.assertTrue(check_started.wait(5))

            clear_result = service.clear_queue()
            self.assertTrue(clear_result['abort_requested'])
            self.assertTrue(service.abort_current_check.is_set())
            self.assertTrue(service._sync_batch_execution_active)

            service.running = True
            service.batch_start_time = None
            service._start_batch_changelog = Mock()
            service._finalize_batch_changelog = Mock()
            service._check_channel = Mock()
            with patch.object(service.check_queue, 'get_next_entry') as get_next_entry, patch(
                'apps.stream.stream_checker_service.time.sleep',
                side_effect=lambda _seconds: setattr(service, 'running', False),
            ):
                service._worker_loop()

            self.assertTrue(service.abort_current_check.is_set())
            get_next_entry.assert_not_called()
            release_check.set()
            sync_thread.join(timeout=5)

        self.assertFalse(sync_thread.is_alive())
        self.assertTrue(thread_result['result'][101]['aborted'])
        self.assertFalse(service._sync_batch_execution_active)
        self.assertFalse(service.check_queue.paused)

    def test_sync_batch_finishes_serial_bitrate_rechecks_before_next_channel(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.side_effect = (
            lambda key, default=None: False
            if key == 'concurrent_streams.enabled'
            else default
        )
        service._require_quality_check_connectivity = Mock(return_value=None)
        events = []

        def check_channel(channel_id, **_kwargs):
            events.append(('initial_scan', channel_id))
            results = [
                {
                    'stream_id': channel_id * 10 + offset,
                    'stream_name': f'{channel_id}-{offset}',
                    'status': 'OK',
                    'bitrate_kbps': None,
                    'measurement_incomplete': True,
                    'measurement_incomplete_reason': 'missing_bitrate',
                    'measurement_incomplete_context': {},
                    'bitrate_recheck_required': True,
                }
                for offset in (1, 2)
            ]
            streams = {
                result['stream_id']: {
                    'id': result['stream_id'],
                    'name': result['stream_name'],
                }
                for result in results
            }

            def recheck(stream, _initial):
                events.append(('bitrate_recheck', channel_id, stream['id']))
                return {
                    'status': 'OK',
                    'bitrate_kbps': 5000 + stream['id'],
                    'bitrate_source': 'ffmpeg_progress',
                }

            service._run_deferred_bitrate_rechecks(results, streams, recheck)
            events.append(('channel_complete', channel_id))
            return {
                'success': True,
                'channel_name': f'Channel {channel_id}',
                'checked_streams': results,
            }

        service._check_channel_concurrent = Mock(side_effect=check_channel)
        udi = Mock()
        udi.get_channel_by_id.side_effect = (
            lambda channel_id: {
                'streams': [
                    {'id': channel_id * 10 + 1},
                    {'id': channel_id * 10 + 2},
                ]
            }
        )

        with patch('apps.udi.get_udi_manager', return_value=udi):
            service.check_channels_synchronously([101, 102])

        self.assertTrue(all(
            call.kwargs.get('global_limit_override') == 1
            for call in service._check_channel_concurrent.call_args_list
        ))

        self.assertEqual(
            events,
            [
                ('initial_scan', 101),
                ('bitrate_recheck', 101, 1011),
                ('bitrate_recheck', 101, 1012),
                ('channel_complete', 101),
                ('initial_scan', 102),
                ('bitrate_recheck', 102, 1021),
                ('bitrate_recheck', 102, 1022),
                ('channel_complete', 102),
            ],
        )

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
        secret = "Traceback with /app/data/secret/path"
        service.clear_queue.return_value = {
            "abort_requested": True,
            "cleared": {"queued": 2, "in_progress": 1},
            "diagnostics": secret,
        }

        with self.app.app_context():
            response = clear_stream_checker_queue_response(
                get_stream_checker_service=lambda: service
            )

        data = response.get_json()
        self.assertEqual(data["message"], "Queue cleared successfully")
        self.assertTrue(data["abort_requested"])
        self.assertEqual(data["cleared"]["in_progress"], 1)
        self.assertNotIn("diagnostics", data)
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_clear_queue_response_rejects_untrusted_public_fields(self):
        secret = "Traceback with /app/data/secret/path"
        invalid_results = (
            {
                "abort_requested": secret,
                "cleared": {"queued": 1},
            },
            {
                "abort_requested": False,
                "cleared": {"queued": secret},
            },
            {
                "abort_requested": False,
                "cleared": {"channel_ids": [True]},
            },
            {
                "abort_requested": False,
                "cleared": {"channel_ids": [secret]},
            },
        )

        for invalid_result in invalid_results:
            with self.subTest(invalid_result=repr(invalid_result)):
                service = Mock()
                service.clear_queue.return_value = invalid_result

                with self.app.app_context():
                    response, status_code = clear_stream_checker_queue_response(
                        get_stream_checker_service=lambda: service,
                    )

                self.assertEqual(status_code, 500)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Internal Server Error"},
                )
                self.assertNotIn("secret", response.get_data(as_text=True))

    def test_guarded_clear_handler_normalizes_snapshot_and_returns_nested_clear(self):
        service = Mock()
        service.clear_queue.return_value = {
            'guard_matched': True,
            'abort_requested': False,
            'cleared': {
                'queued': 2,
                'in_progress': 0,
                'completed': 0,
                'failed': 0,
                'queue_size': 2,
                'channel_ids': [105, 106],
            },
            'current': None,
        }
        snapshot = {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [{
                'channel_id': 105,
                'stream_count': 4,
                'priority': 90,
                'entry_token': 11,
                'metadata': {
                    'source': 'codex_bitrate_recheck',
                    'run_label': 'bitrate-live-r9',
                },
            }, {
                'channel_id': 106,
                'entry_token': 12,
                'metadata': {
                    'source': 'codex_bitrate_recheck',
                    'run_label': 'bitrate-live-r9',
                },
            }],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
            'unrelated_status_field': 'ignored',
        }

        with self.app.app_context():
            response = clear_stream_checker_queue_response(
                payload={'expected_queue_snapshot': snapshot},
                get_stream_checker_service=lambda: service,
            )

        data = response.get_json()
        self.assertEqual(data['message'], 'Queue cleared successfully')
        self.assertTrue(data['guard_matched'])
        self.assertEqual(data['cleared']['channel_ids'], [105, 106])
        normalized = service.clear_queue.call_args.kwargs[
            'expected_queue_snapshot'
        ]
        self.assertNotIn('unrelated_status_field', normalized)
        self.assertIs(normalized['paused'], False)
        self.assertNotIn('stream_count', normalized['queued_entries'][0])
        self.assertEqual(
            normalized['queued_entries'][0]['metadata']['run_label'],
            'bitrate-live-r9',
        )

    def test_guarded_clear_handler_returns_409_without_success_message(self):
        service = Mock()
        service.clear_queue.return_value = {
            'guard_matched': False,
            'guard_failure_reason': 'active_owner_not_in_snapshot',
            'abort_requested': False,
            'cleared': None,
            'current': {
                'entries_complete': True,
                'admission_epoch': 'b' * 32,
                'admission_revision': 8,
                'paused': False,
                'queued_entries': [],
                'in_progress_entries': [],
                'completed_entries': [],
                'failed_entries': [],
                'completed_channel_ids': [],
                'failed_channel_ids': [],
            },
        }
        snapshot = {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }

        with self.app.app_context():
            response, status_code = clear_stream_checker_queue_response(
                payload={'expected_queue_snapshot': snapshot},
                get_stream_checker_service=lambda: service,
            )

        data = response.get_json()
        self.assertEqual(status_code, 409)
        self.assertEqual(data['error'], 'queue_snapshot_mismatch')
        self.assertNotEqual(data['message'], 'Queue cleared successfully')
        self.assertFalse(data['guard_matched'])
        self.assertIsNone(data['cleared'])
        self.assertEqual(
            data["guard_failure_reason"],
            "active_owner_not_in_snapshot",
        )

    def test_guarded_clear_handler_does_not_echo_mismatch_internals(self):
        service = Mock()
        secret = "Traceback with /app/data/secret/path"
        service.clear_queue.return_value = {
            "guard_matched": False,
            "guard_failure_reason": secret,
            "abort_requested": False,
            "cleared": None,
            "current": {
                "entries_complete": True,
                "admission_epoch": "b" * 32,
                "admission_revision": 8,
                "paused": False,
                "queued_entries": [
                    {
                        "channel_id": 105,
                        "entry_token": 11,
                        "metadata": {"traceback": secret},
                    }
                ],
            },
            "message": secret,
        }
        snapshot = {
            "entries_complete": True,
            "admission_epoch": "a" * 32,
            "admission_revision": 7,
            "paused": False,
            "queued_entries": [],
            "in_progress_entries": [],
            "completed_entries": [],
            "failed_entries": [],
            "completed_channel_ids": [],
            "failed_channel_ids": [],
        }

        with self.app.app_context():
            response, status_code = clear_stream_checker_queue_response(
                payload={"expected_queue_snapshot": snapshot},
                get_stream_checker_service=lambda: service,
            )

        self.assertEqual(status_code, 409)
        self.assertEqual(
            response.get_json(),
            {
                "error": "queue_snapshot_mismatch",
                "message": "Queue changed after the expected snapshot was read",
                "guard_matched": False,
                "abort_requested": False,
                "cleared": None,
                "current": None,
                "batch_changelog_finalized": False,
                "batch_changelog_detached": False,
            },
        )
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_guarded_clear_handler_rejects_incomplete_or_invalid_identity(self):
        invalid_snapshots = ({
            'entries_complete': False,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'not-an-epoch',
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [{
                'channel_id': 105,
                'entry_token': True,
                'metadata': {},
            }],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [{
                'channel_id': 105,
                'entry_token': 11,
                'metadata': {'source': 'foreign_run'},
            }],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': 'false',
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            # Missing paused must fail closed even when all identities are
            # otherwise a valid empty guarded snapshot.
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }, {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [{
                'channel_id': 105,
                'entry_token': 11,
                'metadata': {'run_label': '\ud800'},
            }],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        })
        for snapshot in invalid_snapshots:
            with self.subTest(snapshot=snapshot):
                service = Mock()
                with self.app.app_context():
                    response, status_code = clear_stream_checker_queue_response(
                        payload={'expected_queue_snapshot': snapshot},
                        get_stream_checker_service=lambda: service,
                    )

                self.assertEqual(status_code, 400)
                self.assertIn('error', response.get_json())
                service.clear_queue.assert_not_called()

    def test_guarded_clear_handler_bounds_entry_count_and_metadata_depth(self):
        base_snapshot = {
            'entries_complete': True,
            'admission_epoch': 'a' * 32,
            'admission_revision': 7,
            'paused': False,
            'queued_entries': [],
            'in_progress_entries': [],
            'completed_entries': [],
            'failed_entries': [],
            'completed_channel_ids': [],
            'failed_channel_ids': [],
        }
        too_many_entries = dict(base_snapshot)
        too_many_entries['queued_entries'] = [
            {
                'channel_id': index + 1,
                'entry_token': index,
                'metadata': {},
            }
            for index in range(2049)
        ]
        too_many_terminal_ids = dict(base_snapshot)
        too_many_terminal_ids['completed_channel_ids'] = list(range(1, 2050))

        too_wide_list = dict(base_snapshot)
        too_wide_list['queued_entries'] = [{
            'channel_id': 105,
            'entry_token': 11,
            'metadata': {'values': [0] * 513},
        }]
        too_wide_dict = dict(base_snapshot)
        too_wide_dict['queued_entries'] = [{
            'channel_id': 105,
            'entry_token': 11,
            'metadata': {
                f'key_{index}': index
                for index in range(512)
            },
        }]

        nested_metadata = {}
        cursor = nested_metadata
        for index in range(9):
            cursor['child'] = {}
            cursor = cursor['child']
        too_deep = dict(base_snapshot)
        too_deep['queued_entries'] = [{
            'channel_id': 105,
            'entry_token': 11,
            'metadata': nested_metadata,
        }]

        for snapshot in (
            too_many_entries,
            too_many_terminal_ids,
            too_wide_list,
            too_wide_dict,
            too_deep,
        ):
            with self.subTest(snapshot_kind=(
                'entry_count'
                if snapshot is too_many_entries
                else 'terminal_id_count'
                if snapshot is too_many_terminal_ids
                else 'wide_list'
                if snapshot is too_wide_list
                else 'wide_dict'
                if snapshot is too_wide_dict
                else 'depth'
            )):
                service = Mock()
                with self.app.app_context():
                    response, status_code = clear_stream_checker_queue_response(
                        payload={'expected_queue_snapshot': snapshot},
                        get_stream_checker_service=lambda: service,
                    )

                self.assertEqual(status_code, 400)
                self.assertIn('error', response.get_json())
                service.clear_queue.assert_not_called()

    def test_queue_clear_route_bounds_and_strictly_parses_raw_json_body(self):
        from apps.api import web_api

        web_api.app.config['TESTING'] = True
        with patch.object(
            web_api,
            'STREAM_CHECKER_QUEUE_CLEAR_MAX_BODY_BYTES',
            6000,
        ), patch.object(web_api, 'get_stream_checker_service') as service_getter:
            with web_api.app.test_client() as client:
                oversized = client.post(
                    '/api/stream-checker/queue/clear',
                    data=b'x' * 6001,
                    content_type='application/json',
                )
                invalid_json = client.post(
                    '/api/stream-checker/queue/clear',
                    data=b'{invalid',
                    content_type='application/json',
                )
                oversized_integer = client.post(
                    '/api/stream-checker/queue/clear',
                    data=b'1' * 5000,
                    content_type='application/json',
                )
                non_finite_snapshot = {
                    'entries_complete': True,
                    'admission_epoch': 'a' * 32,
                    'admission_revision': 7,
                    'paused': False,
                    'queued_entries': [{
                        'channel_id': 105,
                        'entry_token': 11,
                        'metadata': {'value': float('nan')},
                    }],
                    'in_progress_entries': [],
                    'completed_entries': [],
                    'failed_entries': [],
                    'completed_channel_ids': [],
                    'failed_channel_ids': [],
                }
                non_finite_json = client.post(
                    '/api/stream-checker/queue/clear',
                    data=json.dumps({
                        'expected_queue_snapshot': non_finite_snapshot,
                    }).encode('utf-8'),
                    content_type='application/json',
                )
                large_exponent_json = client.post(
                    '/api/stream-checker/queue/clear',
                    data=json.dumps({
                        'expected_queue_snapshot': non_finite_snapshot,
                    }).replace('NaN', '1e999').encode('utf-8'),
                    content_type='application/json',
                )

        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(oversized_integer.status_code, 400)
        self.assertEqual(non_finite_json.status_code, 400)
        self.assertEqual(large_exponent_json.status_code, 400)
        service_getter.assert_not_called()

    def test_queue_response_propagates_safe_public_ownership_metadata(self):
        service = Mock()
        service.queue_channels.return_value = 2
        metadata = {
            'source': 'codex_bitrate_recheck',
            'run_label': 'bitrate-live-r9',
        }

        with self.app.app_context():
            response = add_to_stream_checker_queue_response(
                payload={
                    'channel_ids': [105, 106],
                    'priority': 90,
                    'force_check': True,
                    'metadata': metadata,
                },
                get_stream_checker_service=lambda: service,
            )

        self.assertEqual(response.get_json()['added'], 2)
        service.queue_channels.assert_called_once_with(
            [105, 106],
            90,
            force_check=True,
            metadata=metadata,
            immutable_metadata_keys=set(metadata),
        )

    def test_queue_response_coerces_string_false_force_flag(self):
        service = Mock()
        service.queue_channels.return_value = 1

        with self.app.app_context():
            response = add_to_stream_checker_queue_response(
                payload={
                    'channel_ids': [105],
                    'force_check': 'false',
                },
                get_stream_checker_service=lambda: service,
            )

        self.assertEqual(response.get_json()['added'], 1)
        service.queue_channels.assert_called_once_with(
            [105],
            10,
            force_check=False,
        )

    def test_queue_response_rejects_untrusted_service_counts(self):
        secret = "Traceback with /app/data/secret/path"
        for invalid_count in (secret, {"exception": secret}, True, -1, 1.5, 3):
            with self.subTest(invalid_count=repr(invalid_count)):
                service = Mock()
                service.queue_channels.return_value = invalid_count

                with self.app.app_context():
                    response, status_code = add_to_stream_checker_queue_response(
                        payload={"channel_ids": [105, 106]},
                        get_stream_checker_service=lambda: service,
                    )

                self.assertEqual(status_code, 500)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Internal Server Error"},
                )
                self.assertNotIn("secret", response.get_data(as_text=True))

    def test_queue_response_rejects_invalid_channel_id_arrays(self):
        for invalid_channel_ids in (
            True,
            "105",
            {"channel_id": 105},
            [True],
            ["105"],
            [0],
            [-1],
        ):
            with self.subTest(channel_ids=repr(invalid_channel_ids)):
                service = Mock()

                with self.app.app_context():
                    response, status_code = add_to_stream_checker_queue_response(
                        payload={"channel_ids": invalid_channel_ids},
                        get_stream_checker_service=lambda: service,
                    )

                self.assertEqual(status_code, 400)
                self.assertEqual(
                    response.get_json(),
                    {"error": "channel_ids must be an array of positive integers"},
                )
                service.queue_channels.assert_not_called()

    def test_queue_response_rejects_reserved_or_unbounded_metadata(self):
        for metadata in (
            {'source': 'teamarr_preflight', 'run_label': 'forged'},
            {'source': 'codex_bitrate_recheck', 'unexpected': 'value'},
            {'source': 'contains spaces'},
        ):
            service = Mock()
            with self.app.app_context():
                response, status_code = add_to_stream_checker_queue_response(
                    payload={
                        'channel_ids': [105, 106],
                        'metadata': metadata,
                    },
                    get_stream_checker_service=lambda: service,
                )

            self.assertEqual(status_code, 400)
            self.assertIn('error', response.get_json())
            service.queue_channels.assert_not_called()

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

    def test_queue_all_channels_rejects_untrusted_service_counts(self):
        secret = "Traceback with /app/data/secret/path"
        for invalid_count in (secret, {"exception": secret}, True, -1, 1.5, 4):
            with self.subTest(invalid_count=repr(invalid_count)):
                service = Mock()
                service.config = Mock()
                service.config.get.side_effect = lambda key, default=None: default
                service.update_tracker = Mock()
                service.queue_channels.return_value = invalid_count
                udi = Mock()
                udi.get_channels.return_value = [
                    {"id": 1},
                    {"id": 2},
                    {"id": 3},
                ]

                with self.app.app_context():
                    response, status_code = queue_all_channels_response(
                        get_stream_checker_service=lambda: service,
                        get_udi_manager=lambda: udi,
                    )

                self.assertEqual(status_code, 500)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Internal Server Error"},
                )
                self.assertNotIn("secret", response.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main(verbosity=2)

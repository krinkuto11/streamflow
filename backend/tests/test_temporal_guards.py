#!/usr/bin/env python3
"""
Tests for temporal guards in SchedulingService.

Covers three layers:
  Layer 1 — create_scheduled_event(): creation-time validation
  Layer 2 — _load_scheduled_events(): load-time staleness filter
  Layer 3 — get_due_events(): execution-time staleness guard

Key scenario: container-restart use case — event was valid when created,
but program aired while the container was down.
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

# Bootstrap environment before any app imports
os.environ.setdefault('CONFIG_DIR', tempfile.mkdtemp())
os.environ.setdefault('DISPATCHARR_BASE_URL', 'http://test.local')
os.environ.setdefault('DISPATCHARR_TOKEN', 'test_token')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apps.automation.scheduling_service as scheduling_service_module
from apps.automation.scheduling_service import SchedulingService, _parse_dt


# ---------------------------------------------------------------------------
# Shared test base
# ---------------------------------------------------------------------------

class _SchedulingBase(unittest.TestCase):
    """Shared setUp/tearDown for tests that need a fresh SchedulingService."""

    def setUp(self):
        self.test_config_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_config_dir

        # Reset module-level state
        scheduling_service_module._scheduling_service = None
        scheduling_service_module.CONFIG_DIR = Path(self.test_config_dir)
        scheduling_service_module.SCHEDULING_CONFIG_FILE = (
            scheduling_service_module.CONFIG_DIR / 'scheduling_config.json'
        )
        scheduling_service_module.SCHEDULED_EVENTS_FILE = (
            scheduling_service_module.CONFIG_DIR / 'scheduled_events.json'
        )
        scheduling_service_module.AUTO_CREATE_RULES_FILE = (
            scheduling_service_module.CONFIG_DIR / 'auto_create_rules.json'
        )
        scheduling_service_module.EXECUTED_EVENTS_FILE = (
            scheduling_service_module.CONFIG_DIR / 'executed_events.json'
        )

        self.service = SchedulingService()

        # Patch UDI
        self.mock_udi_patcher = patch('apps.automation.scheduling_service.get_udi_manager')
        self.mock_udi = self.mock_udi_patcher.start()
        self.mock_channel = {
            'id': 1,
            'name': 'Test Channel',
            'tvg_id': 'test-channel-1',
            'logo_id': None,
        }
        self.mock_udi.return_value.get_channel_by_id.return_value = self.mock_channel

    def tearDown(self):
        self.mock_udi_patcher.stop()
        scheduling_service_module._scheduling_service = None
        shutil.rmtree(self.test_config_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _future_event(self, hours_until_start=2, duration_hours=2, minutes_before=5, **overrides):
        """Return a valid event_data dict for a future program."""
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=hours_until_start)
        end = start + timedelta(hours=duration_hours)
        data = {
            'channel_id': 1,
            'program_title': 'Test Program',
            'program_start_time': start.isoformat(),
            'program_end_time': end.isoformat(),
            'minutes_before': minutes_before,
        }
        data.update(overrides)
        return data

    def _inject_raw_events(self, events):
        """Bypass service logic to inject raw event dicts directly into the DB."""
        self.service._scheduled_events = events
        self.service._save_scheduled_events()

    def _fresh_service(self):
        """Return a new SchedulingService that loads from the same DB."""
        scheduling_service_module._scheduling_service = None
        svc = SchedulingService()
        # Re-attach the UDI mock
        return svc


# ===========================================================================
# Layer 1 — create_scheduled_event() creation-time guards
# ===========================================================================

class TestCreationTimeGuards(_SchedulingBase):

    def test_valid_future_event_is_accepted(self):
        """A well-formed future event must be created without error."""
        event = self.service.create_scheduled_event(self._future_event())
        self.assertIn('id', event)
        self.assertEqual(len(self.service._scheduled_events), 1)

    def test_fully_ended_program_is_rejected(self):
        """An event whose program has already ended must be rejected with ValueError."""
        now = datetime.now(timezone.utc)
        data = {
            'channel_id': 1,
            'program_title': 'Old Show',
            'program_start_time': (now - timedelta(hours=3)).isoformat(),
            'program_end_time': (now - timedelta(hours=1)).isoformat(),
            'minutes_before': 5,
        }
        with self.assertRaises(ValueError) as ctx:
            self.service.create_scheduled_event(data)
        self.assertIn('already aired', str(ctx.exception))
        self.assertEqual(len(self.service._scheduled_events), 0)

    def test_program_ended_exactly_now_is_rejected(self):
        """A program whose end time is exactly now (boundary) must be rejected."""
        now = datetime.now(timezone.utc)
        data = {
            'channel_id': 1,
            'program_title': 'Boundary Show',
            'program_start_time': (now - timedelta(hours=1)).isoformat(),
            # Use a time 1 second in the past to reliably test boundary
            'program_end_time': (now - timedelta(seconds=1)).isoformat(),
            'minutes_before': 0,
        }
        with self.assertRaises(ValueError):
            self.service.create_scheduled_event(data)

    def test_end_before_start_is_rejected(self):
        """An event where end_time <= start_time must be rejected."""
        now = datetime.now(timezone.utc)
        data = {
            'channel_id': 1,
            'program_title': 'Backwards Show',
            'program_start_time': (now + timedelta(hours=2)).isoformat(),
            'program_end_time': (now + timedelta(hours=1)).isoformat(),
            'minutes_before': 0,
        }
        with self.assertRaises(ValueError) as ctx:
            self.service.create_scheduled_event(data)
        self.assertIn('after program_start_time', str(ctx.exception))

    def test_currently_airing_program_is_accepted_and_fires_immediately(self):
        """A program that is currently airing (start in past, end in future) must
        be accepted — the check should execute immediately on the next cycle."""
        now = datetime.now(timezone.utc)
        data = {
            'channel_id': 1,
            'program_title': 'Live Match',
            'program_start_time': (now - timedelta(minutes=30)).isoformat(),
            'program_end_time': (now + timedelta(hours=1, minutes=30)).isoformat(),
            'minutes_before': 0,
        }
        event = self.service.create_scheduled_event(data)
        self.assertIn('id', event)

        # check_time must be in the past so get_due_events() returns it immediately
        check_dt = _parse_dt(event['check_time'])
        self.assertLessEqual(check_dt, now)

        due = self.service.get_due_events()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]['id'], event['id'])

    def test_large_minutes_before_creates_past_check_time_but_is_accepted(self):
        """minutes_before larger than lead time puts check_time in the past.
        The event should still be accepted (program is still in the future)."""
        now = datetime.now(timezone.utc)
        data = {
            'channel_id': 1,
            'program_title': 'Near Show',
            'program_start_time': (now + timedelta(minutes=3)).isoformat(),
            'program_end_time': (now + timedelta(hours=1, minutes=3)).isoformat(),
            'minutes_before': 10,  # 10 min before a show that starts in 3 min
        }
        event = self.service.create_scheduled_event(data)
        self.assertIn('id', event)
        # check_time should be 7 minutes in the past
        check_dt = _parse_dt(event['check_time'])
        self.assertLess(check_dt, now)

    def test_invalid_channel_raises_value_error(self):
        """An unknown channel_id must raise ValueError."""
        self.mock_udi.return_value.get_channel_by_id.return_value = None
        with self.assertRaises(ValueError) as ctx:
            self.service.create_scheduled_event(self._future_event())
        self.assertIn('not found', str(ctx.exception))


# ===========================================================================
# Layer 2 — _load_scheduled_events() load-time staleness filter
# ===========================================================================

class TestLoadTimeStalenessFilter(_SchedulingBase):

    def test_stale_event_is_pruned_on_load(self):
        """An event whose program has ended is removed from the queue on load
        (simulates a container restart after the program aired)."""
        now = datetime.now(timezone.utc)

        stale_event = {
            'id': 'stale-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Stale Program',
            'program_start_time': (now - timedelta(hours=4)).isoformat(),
            'program_end_time': (now - timedelta(hours=2)).isoformat(),
            'check_time': (now - timedelta(hours=4, minutes=5)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([stale_event])

        # Simulate container restart
        svc = self._fresh_service()

        self.assertEqual(len(svc._scheduled_events), 0,
                         "Stale event should be pruned on load")

    def test_valid_future_event_survives_load(self):
        """An event whose program is still in the future must survive the load filter."""
        now = datetime.now(timezone.utc)

        valid_event = {
            'id': 'valid-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Future Program',
            'program_start_time': (now + timedelta(hours=1)).isoformat(),
            'program_end_time': (now + timedelta(hours=3)).isoformat(),
            'check_time': (now + timedelta(minutes=55)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([valid_event])

        svc = self._fresh_service()

        self.assertEqual(len(svc._scheduled_events), 1)
        self.assertEqual(svc._scheduled_events[0]['id'], 'valid-001')

    def test_mix_of_stale_and_valid_events_on_load(self):
        """Only stale events are pruned; valid events survive the load filter."""
        now = datetime.now(timezone.utc)

        stale = {
            'id': 'stale-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Old Show',
            'program_start_time': (now - timedelta(hours=3)).isoformat(),
            'program_end_time': (now - timedelta(hours=1)).isoformat(),
            'check_time': (now - timedelta(hours=3, minutes=5)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        valid = {
            'id': 'valid-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Upcoming Show',
            'program_start_time': (now + timedelta(hours=2)).isoformat(),
            'program_end_time': (now + timedelta(hours=4)).isoformat(),
            'check_time': (now + timedelta(hours=1, minutes=55)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([stale, valid])

        svc = self._fresh_service()

        ids = [e['id'] for e in svc._scheduled_events]
        self.assertNotIn('stale-001', ids)
        self.assertIn('valid-001', ids)

    def test_container_restart_scenario(self):
        """Primary use case: event was valid at creation time, container was stopped,
        program aired during downtime — event must be pruned on restart."""
        now = datetime.now(timezone.utc)

        # Simulate what the DB looks like after a crash/stop with a pending event
        pending_event = {
            'id': 'pending-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Champions League Final',
            'program_start_time': (now - timedelta(hours=2)).isoformat(),
            'program_end_time': (now - timedelta(minutes=15)).isoformat(),
            'check_time': (now - timedelta(hours=2, minutes=10)).isoformat(),
            'minutes_before': 10,
            'schedule_type': 'check',
        }
        self._inject_raw_events([pending_event])

        # Container restart
        svc = self._fresh_service()

        self.assertEqual(len(svc._scheduled_events), 0,
                         "Event that missed its window during downtime must be purged on restart")

        # Also confirm the processor would find nothing due
        due = svc.get_due_events()
        self.assertEqual(len(due), 0)

    def test_currently_airing_event_survives_load(self):
        """An event for a program that is currently airing (started but not ended)
        must survive the load filter — it should still execute."""
        now = datetime.now(timezone.utc)

        airing_event = {
            'id': 'airing-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Live News',
            'program_start_time': (now - timedelta(minutes=20)).isoformat(),
            'program_end_time': (now + timedelta(minutes=40)).isoformat(),
            'check_time': (now - timedelta(minutes=25)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([airing_event])

        svc = self._fresh_service()

        self.assertEqual(len(svc._scheduled_events), 1,
                         "Currently-airing event must survive load (program not yet ended)")

    def test_pruned_events_are_persisted_immediately(self):
        """After pruning stale events on load, the cleaned list must be written
        back to the DB so the next restart doesn't re-encounter them."""
        now = datetime.now(timezone.utc)

        stale = {
            'id': 'stale-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Old Show',
            'program_start_time': (now - timedelta(hours=3)).isoformat(),
            'program_end_time': (now - timedelta(hours=1)).isoformat(),
            'check_time': (now - timedelta(hours=3, minutes=5)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([stale])

        # First restart — prunes the stale event
        svc1 = self._fresh_service()
        self.assertEqual(len(svc1._scheduled_events), 0)

        # Second restart — must load an already-clean list (not re-read the stale one)
        svc2 = self._fresh_service()
        self.assertEqual(len(svc2._scheduled_events), 0,
                         "Pruned events must not reappear on subsequent restarts")

    def test_event_without_end_time_survives_load(self):
        """An event missing program_end_time is kept (no end time = cannot determine
        staleness at load; execution-time guard handles it)."""
        now = datetime.now(timezone.utc)

        no_end_event = {
            'id': 'noend-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'No End Time Show',
            'program_start_time': (now - timedelta(hours=1)).isoformat(),
            # No program_end_time key
            'check_time': (now - timedelta(hours=1, minutes=5)).isoformat(),
            'minutes_before': 5,
            'schedule_type': 'check',
        }
        self._inject_raw_events([no_end_event])

        svc = self._fresh_service()

        self.assertEqual(len(svc._scheduled_events), 1,
                         "Event without end time must survive load filter")


# ===========================================================================
# Layer 3 — get_due_events() execution-time staleness guard
# ===========================================================================

class TestExecutionTimeStalenessGuard(_SchedulingBase):

    def _inject_live(self, events):
        """Directly set _scheduled_events in memory without going through DB load."""
        self.service._scheduled_events = events

    def test_due_event_with_live_program_is_returned(self):
        """A due event whose program is still airing must be returned."""
        now = datetime.now(timezone.utc)

        event = {
            'id': 'due-live-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Live Show',
            'program_start_time': (now - timedelta(minutes=10)).isoformat(),
            'program_end_time': (now + timedelta(hours=1)).isoformat(),
            'check_time': (now - timedelta(minutes=1)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([event])

        due = self.service.get_due_events()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]['id'], 'due-live-001')

    def test_due_event_with_ended_program_is_skipped(self):
        """A due event whose program has ended must be skipped and purged."""
        now = datetime.now(timezone.utc)

        event = {
            'id': 'due-stale-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Missed Show',
            'program_start_time': (now - timedelta(hours=3)).isoformat(),
            'program_end_time': (now - timedelta(hours=1)).isoformat(),
            'check_time': (now - timedelta(hours=3, minutes=5)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([event])

        due = self.service.get_due_events()

        self.assertEqual(len(due), 0,
                         "Stale event with ended program must not be returned as due")
        self.assertEqual(len(self.service._scheduled_events), 0,
                         "Stale event must be purged from queue after detection")

    def test_not_yet_due_event_is_not_returned(self):
        """A future event (check_time not yet reached) must not be returned."""
        now = datetime.now(timezone.utc)

        event = {
            'id': 'future-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Future Show',
            'program_start_time': (now + timedelta(hours=2)).isoformat(),
            'program_end_time': (now + timedelta(hours=4)).isoformat(),
            'check_time': (now + timedelta(hours=1, minutes=55)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([event])

        due = self.service.get_due_events()
        self.assertEqual(len(due), 0)
        # Must NOT be purged — it just isn't due yet
        self.assertEqual(len(self.service._scheduled_events), 1)

    def test_event_went_stale_while_in_queue(self):
        """Simulates an event that was valid when loaded but whose program ended
        while the container was running (e.g. processor paused or heavily loaded)."""
        now = datetime.now(timezone.utc)

        # Event was valid at load time but program has since ended
        stale_in_queue = {
            'id': 'stale-in-queue-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Expired Show',
            'program_start_time': (now - timedelta(hours=2)).isoformat(),
            'program_end_time': (now - timedelta(minutes=5)).isoformat(),
            'check_time': (now - timedelta(hours=2, minutes=5)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([stale_in_queue])

        due = self.service.get_due_events()
        self.assertEqual(len(due), 0)
        self.assertEqual(len(self.service._scheduled_events), 0,
                         "Event that expired while in queue must be purged")

    def test_mixed_due_and_stale_events(self):
        """Only live due events are returned; stale ones are silently purged."""
        now = datetime.now(timezone.utc)

        live_due = {
            'id': 'live-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Live Match',
            'program_start_time': (now - timedelta(minutes=5)).isoformat(),
            'program_end_time': (now + timedelta(hours=2)).isoformat(),
            'check_time': (now - timedelta(minutes=1)).isoformat(),
            'minutes_before': 5,
        }
        stale_due = {
            'id': 'stale-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Old Film',
            'program_start_time': (now - timedelta(hours=4)).isoformat(),
            'program_end_time': (now - timedelta(hours=2)).isoformat(),
            'check_time': (now - timedelta(hours=4, minutes=5)).isoformat(),
            'minutes_before': 5,
        }
        future = {
            'id': 'future-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'Upcoming Show',
            'program_start_time': (now + timedelta(hours=3)).isoformat(),
            'program_end_time': (now + timedelta(hours=5)).isoformat(),
            'check_time': (now + timedelta(hours=2, minutes=55)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([live_due, stale_due, future])

        due = self.service.get_due_events()

        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]['id'], 'live-001')

        remaining_ids = [e['id'] for e in self.service._scheduled_events]
        self.assertIn('future-001', remaining_ids)
        self.assertNotIn('stale-001', remaining_ids)
        self.assertIn('live-001', remaining_ids)  # Still in queue until executed

    def test_event_without_end_time_is_still_returned_as_due(self):
        """An event missing program_end_time cannot be staleness-checked at execution
        time, so it should still be returned as due (allow the check to run)."""
        now = datetime.now(timezone.utc)

        event = {
            'id': 'noend-due-001',
            'channel_id': 1,
            'channel_name': 'Test Channel',
            'program_title': 'No End Time Show',
            'program_start_time': (now - timedelta(hours=1)).isoformat(),
            # No program_end_time
            'check_time': (now - timedelta(minutes=5)).isoformat(),
            'minutes_before': 5,
        }
        self._inject_live([event])

        due = self.service.get_due_events()
        self.assertEqual(len(due), 1,
                         "Event without end time must still fire — cannot determine staleness")


# ===========================================================================
# Regression: existing behaviour must not be broken
# ===========================================================================

class TestExistingBehaviourPreserved(_SchedulingBase):
    """Quick regression smoke-tests to confirm existing passing behaviour
    is not broken by the new guards."""

    def test_auto_create_rule_flow_still_skips_past_programs(self):
        """match_programs_to_rules must still skip programs that have started."""
        now = datetime.now(timezone.utc)

        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': '^Test',
            'minutes_before': 5,
        }
        with patch.object(self.service, 'match_programs_to_rules'):
            self.service.create_auto_create_rule(rule_data)

        # Both programs started in the past
        self.service._epg_cache = [
            {
                'title': 'Test Show Past',
                'start_time': (now - timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=1)).isoformat(),
                'tvg_id': 'test-channel-1',
            },
            {
                'title': 'Test Show Future',
                'start_time': (now + timedelta(hours=2)).isoformat(),
                'end_time': (now + timedelta(hours=4)).isoformat(),
                'tvg_id': 'test-channel-1',
            },
        ]
        self.service._epg_cache_time = now

        with patch.object(self.service, 'fetch_channel_programs_from_api',
                          return_value=self.service._epg_cache):
            result = self.service.match_programs_to_rules()

        self.assertEqual(result['created'], 1)
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['program_title'], 'Test Show Future')

    def test_delete_scheduled_event_still_works(self):
        """delete_scheduled_event must still remove an event by ID."""
        event = self.service.create_scheduled_event(self._future_event())
        event_id = event['id']
        self.assertEqual(len(self.service._scheduled_events), 1)

        result = self.service.delete_scheduled_event(event_id)
        self.assertTrue(result)
        self.assertEqual(len(self.service._scheduled_events), 0)

    def test_get_scheduled_events_sorted_by_check_time(self):
        """get_scheduled_events must return events sorted by check_time."""
        now = datetime.now(timezone.utc)
        event_a = self.service.create_scheduled_event(
            self._future_event(hours_until_start=4, program_title='Late Show')
        )
        event_b = self.service.create_scheduled_event(
            self._future_event(hours_until_start=2, program_title='Early Show')
        )

        events = self.service.get_scheduled_events()
        self.assertEqual(events[0]['id'], event_b['id'],
                         "Earlier check_time must come first")
        self.assertEqual(events[1]['id'], event_a['id'])

    def _future_event(self, hours_until_start=2, duration_hours=2,
                      minutes_before=5, program_title='Test Program', **overrides):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=hours_until_start)
        end = start + timedelta(hours=duration_hours)
        data = {
            'channel_id': 1,
            'program_title': program_title,
            'program_start_time': start.isoformat(),
            'program_end_time': end.isoformat(),
            'minutes_before': minutes_before,
        }
        data.update(overrides)
        return data


if __name__ == '__main__':
    unittest.main(verbosity=2)

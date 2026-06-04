#!/usr/bin/env python3
"""
Unit tests for EPG past events handling.

Tests cover:
1. Programs that are currently airing should still create immediate events
2. Programs in the past should not create events
3. Events should not be re-created after being checked
4. Program name changes should not affect duplicate detection (same start time)
5. Events with start times within 5 minutes should be treated as duplicates
"""

import unittest
import tempfile
import json
import os
import sys
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone

# Set up initial CONFIG_DIR before importing scheduling_service
os.environ['CONFIG_DIR'] = tempfile.mkdtemp()
os.environ['DISPATCHARR_BASE_URL'] = 'http://test.local'
os.environ['DISPATCHARR_TOKEN'] = 'test_token'

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the module
import apps.automation.scheduling_service
from apps.automation.scheduling_service import SchedulingService


class TestEPGPastEvents(unittest.TestCase):
    """Test EPG handling of past events and edge cases."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create a fresh temp directory for each test
        self.test_config_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_config_dir
        
        # Reset the global singleton and reload CONFIG_DIR
        scheduling_service._scheduling_service = None
        scheduling_service.CONFIG_DIR = Path(self.test_config_dir)
        scheduling_service.SCHEDULING_CONFIG_FILE = scheduling_service.CONFIG_DIR / 'scheduling_config.json'
        scheduling_service.SCHEDULED_EVENTS_FILE = scheduling_service.CONFIG_DIR / 'scheduled_events.json'
        scheduling_service.AUTO_CREATE_RULES_FILE = scheduling_service.CONFIG_DIR / 'auto_create_rules.json'
        scheduling_service.EXECUTED_EVENTS_FILE = scheduling_service.CONFIG_DIR / 'executed_events.json'
        
        self.service = SchedulingService()
        
        # Mock UDI manager
        self.mock_udi_patcher = patch('scheduling_service.get_udi_manager')
        self.mock_udi = self.mock_udi_patcher.start()
        
        # Mock channel
        self.mock_channel = {
            'id': 1,
            'name': 'Test Channel',
            'tvg_id': 'test-channel-1',
            'logo_id': None
        }
        
        self.mock_udi.return_value.get_channel_by_id.return_value = self.mock_channel
        self.mock_udi.return_value.get_logo_by_id.return_value = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_udi_patcher.stop()
        # Clean up the temp directory
        if os.path.exists(self.test_config_dir):
            shutil.rmtree(self.test_config_dir)
        
        # Reset the global singleton
        scheduling_service._scheduling_service = None
    
    def test_schedule_programs_currently_airing(self):
        """Test that programs already started but not ended are still scheduled."""
        now = datetime.now(timezone.utc)
        
        # Create a rule
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': '^Test Program',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        # Create EPG programs - one currently airing, one in the future
        programs = [
            {
                'title': 'Test Program Past',
                'start_time': (now - timedelta(minutes=5)).isoformat(),
                'end_time': (now + timedelta(minutes=25)).isoformat(),
                'tvg_id': 'test-channel-1'
            },
            {
                'title': 'Test Program Future',
                'start_time': (now + timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=2)).isoformat(),
                'tvg_id': 'test-channel-1'
            }
        ]
        
        # Mock EPG cache
        self.service._epg_cache = programs
        self.service._epg_cache_time = datetime.now()
        
        # Run matching
        result = self.service.match_programs_to_rules()
        
        # Verify both current and future programs created events
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 2, "Should create events for current and future programs")
        self.assertEqual({event['program_title'] for event in events}, {
            'Test Program Past',
            'Test Program Future',
        })
        self.assertEqual(result['created'], 2)
    
    def test_skip_programs_in_past(self):
        """Test that programs completely in the past are not scheduled."""
        now = datetime.now(timezone.utc)
        
        # Create a rule
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': '^Old Show',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        # Create EPG programs - all in the past
        programs = [
            {
                'title': 'Old Show Episode 1',
                'start_time': (now - timedelta(hours=2)).isoformat(),
                'end_time': (now - timedelta(hours=1)).isoformat(),
                'tvg_id': 'test-channel-1'
            },
            {
                'title': 'Old Show Episode 2',
                'start_time': (now - timedelta(hours=1)).isoformat(),
                'end_time': (now - timedelta(minutes=30)).isoformat(),
                'tvg_id': 'test-channel-1'
            }
        ]
        
        # Mock EPG cache
        self.service._epg_cache = programs
        self.service._epg_cache_time = datetime.now()
        
        # Run matching
        result = self.service.match_programs_to_rules()
        
        # Verify no events were created
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 0, "Should not create events for past programs")
        self.assertEqual(result['created'], 0)
    
    def test_no_recreation_after_execution(self):
        """Test that events are not re-created after being executed."""
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(hours=1)
        
        # Create a rule
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': '^News',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        # Create an EPG program
        program = {
            'title': 'News at 5',
            'start_time': future_time.isoformat(),
            'end_time': (future_time + timedelta(hours=1)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        # Mock EPG cache
        self.service._epg_cache = [program]
        self.service._epg_cache_time = datetime.now()
        
        # Run matching - should create an event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 1, "Should create initial event")
        self.assertEqual(result['created'], 1)
        
        # Get the event ID
        event_id = events[0]['id']
        
        # Mock stream checker
        mock_stream_checker = Mock()
        mock_stream_checker.check_single_channel.return_value = {'success': True}
        
        # Execute the event (this should record it as executed)
        success = self.service.execute_scheduled_check(event_id, mock_stream_checker)
        self.assertTrue(success, "Event execution should succeed")
        
        # Verify event was removed
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 0, "Event should be removed after execution")
        
        # Run matching again - should NOT re-create the event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 0, "Should not re-create executed event")
        self.assertEqual(result['created'], 0)
        self.assertEqual(result['skipped'], 0)

    def test_scheduled_check_defers_while_stream_checker_active(self):
        """Scheduled EPG checks must not run in parallel with active Teamarr/single checks."""
        now = datetime.now(timezone.utc)
        event = self.service.create_scheduled_event({
            'channel_id': 1,
            'program_title': 'Live Baseball',
            'program_start_time': (now + timedelta(minutes=5)).isoformat(),
            'program_end_time': (now + timedelta(hours=2)).isoformat(),
            'minutes_before': 0,
        })

        mock_stream_checker = Mock()
        mock_stream_checker.get_status.return_value = {
            'stream_checking_mode': True,
            'active_single_channel_checks': 1,
            'queue': {'queue_size': 0, 'in_progress': 0},
        }
        mock_stream_checker.check_single_channel.return_value = {'success': True}

        success = self.service.execute_scheduled_check(event['id'], mock_stream_checker)

        self.assertFalse(success)
        mock_stream_checker.check_single_channel.assert_not_called()
        self.assertEqual(
            [scheduled['id'] for scheduled in self.service.get_scheduled_events()],
            [event['id']],
            "Deferred event should remain scheduled for the next processor tick",
        )

        mock_stream_checker.get_status.return_value = {
            'stream_checking_mode': False,
            'active_single_channel_checks': 0,
            'queue': {'queue_size': 0, 'in_progress': 0},
        }

        success = self.service.execute_scheduled_check(event['id'], mock_stream_checker)

        self.assertTrue(success)
        mock_stream_checker.check_single_channel.assert_called_once_with(
            1,
            program_name='Live Baseball',
        )
        self.assertEqual(self.service.get_scheduled_events(), [])
    
    def test_program_name_change_same_time(self):
        """Test that program name changes don't create duplicate events for same time."""
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(hours=1)
        
        # Create a rule that matches both names
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': 'Sports|Football',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        # Create initial program
        program1 = {
            'title': 'Sports Tonight',
            'start_time': future_time.isoformat(),
            'end_time': (future_time + timedelta(hours=1)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        # Mock EPG cache
        self.service._epg_cache = [program1]
        self.service._epg_cache_time = datetime.now()
        
        # Run matching - should create an event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 1, "Should create initial event")
        self.assertEqual(events[0]['program_title'], 'Sports Tonight')
        
        # Now simulate EPG refresh with name change (same time)
        program2 = {
            'title': 'Football Special',
            'start_time': future_time.isoformat(),  # Same start time!
            'end_time': (future_time + timedelta(hours=1)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        self.service._epg_cache = [program2]
        
        # Run matching again - should update existing event, not create new one
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 1, "Should still have only one event")
        self.assertEqual(events[0]['program_title'], 'Football Special', "Title should be updated")
        self.assertEqual(result['updated'], 1)
        self.assertEqual(result['created'], 0)
    
    def test_duplicate_detection_within_5_minutes(self):
        """Test that events with start times within 5 minutes are treated as duplicates."""
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(hours=1)
        
        # Create a rule
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': 'News',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        # Create first program
        program1 = {
            'title': 'News at 5:00',
            'start_time': future_time.isoformat(),
            'end_time': (future_time + timedelta(hours=1)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        # Mock EPG cache
        self.service._epg_cache = [program1]
        self.service._epg_cache_time = datetime.now()
        
        # Run matching - should create an event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 1, "Should create initial event")
        
        # Create second program 3 minutes later (within 5-minute window)
        program2 = {
            'title': 'News at 5:03',
            'start_time': (future_time + timedelta(minutes=3)).isoformat(),
            'end_time': (future_time + timedelta(hours=1, minutes=3)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        # Replace EPG cache with only program2 (simulating EPG update)
        self.service._epg_cache = [program2]
        
        # Run matching again - exact start time is the de-duplication key, so
        # nearby programs are preserved as distinct events.
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 2, "Should preserve nearby programs as separate events")
        self.assertEqual(result['created'], 1)
        
        # Create third program 10 minutes after the original (outside 5-minute window from original)
        program3 = {
            'title': 'News at 5:10',
            'start_time': (future_time + timedelta(minutes=10)).isoformat(),
            'end_time': (future_time + timedelta(hours=1, minutes=10)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        # Now have both the updated program and the new program
        self.service._epg_cache = [program2, program3]
        
        # Run matching again - should create a second event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 3, "Should create another event for the distinct start time")
        self.assertEqual(result['created'], 1)
    
    def test_executed_events_prevent_recreation(self):
        """Test that executed events are tracked and prevent re-creation."""
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(hours=1)
        
        # Manually record an executed event
        with self.service._lock:
            self.service._record_executed_event(1, future_time.isoformat())
        
        # Verify it's recorded
        self.assertTrue(
            self.service._is_event_executed(1, future_time.isoformat()),
            "Event should be marked as executed"
        )
        
        # Try to create the same event via matching
        rule_data = {
            'name': 'Test Rule',
            'channel_id': 1,
            'regex_pattern': 'News',
            'minutes_before': 5
        }
        
        with patch.object(self.service, 'match_programs_to_rules'):
            rule = self.service.create_auto_create_rule(rule_data)
        
        program = {
            'title': 'News at 5',
            'start_time': future_time.isoformat(),
            'end_time': (future_time + timedelta(hours=1)).isoformat(),
            'tvg_id': 'test-channel-1'
        }
        
        self.service._epg_cache = [program]
        self.service._epg_cache_time = datetime.now()
        
        # Run matching - should NOT create event
        result = self.service.match_programs_to_rules()
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 0, "Should not create event for executed program")
        self.assertEqual(result['created'], 0)
    
    def test_executed_events_retention(self):
        """Test that old executed events are cleaned up."""
        now = datetime.now(timezone.utc)
        old_time = now - timedelta(days=8)  # Older than 7 day retention
        recent_time = now - timedelta(days=3)  # Within retention period
        
        # Create executed events
        old_event = {
            'channel_id': 1,
            'program_start_time': old_time.isoformat(),
            'executed_at': old_time.isoformat()
        }
        
        recent_event = {
            'channel_id': 1,
            'program_start_time': recent_time.isoformat(),
            'executed_at': recent_time.isoformat()
        }
        
        # Save both events
        self.service._executed_events = [old_event, recent_event]
        self.service._save_executed_events()
        
        # Create a new service instance (which will load and clean executed events)
        scheduling_service._scheduling_service = None
        new_service = SchedulingService()
        
        # Verify old event was cleaned up
        executed_events = new_service._executed_events
        self.assertEqual(len(executed_events), 1, "Should only have recent event after cleanup")
        self.assertEqual(
            executed_events[0]['program_start_time'],
            recent_time.isoformat(),
            "Should keep the recent event"
        )


if __name__ == '__main__':
    unittest.main()

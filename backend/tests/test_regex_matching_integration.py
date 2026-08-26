#!/usr/bin/env python3
"""
Integration test for regex matching system to verify the deadlock fix.

This test ensures:
1. No deadlock occurs when creating auto-create rules
2. Programs are matched correctly after rule creation
3. EPG data is fetched before matching
"""

import unittest
import pytest

pytestmark = pytest.mark.integration
import tempfile
import json
import os
import sys
import time
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone

# Set minimal environment before importing scheduling_service
os.environ['DISPATCHARR_BASE_URL'] = 'http://test.local'
os.environ['DISPATCHARR_TOKEN'] = 'test_token'

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import apps.automation.scheduling_service
from apps.automation.scheduling_service import SchedulingService


class TestRegexMatchingIntegration(unittest.TestCase):
    """Integration test for regex matching system."""
    
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
        
        # Mock EPG programs
        now = datetime.now(timezone.utc)
        self.mock_programs = [
            {
                'title': 'Breaking News at 5',
                'start_time': (now + timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=2)).isoformat(),
                'tvg_id': 'test-channel-1'
            },
            {
                'title': 'Regular Show',
                'start_time': (now + timedelta(hours=2)).isoformat(),
                'end_time': (now + timedelta(hours=3)).isoformat(),
                'tvg_id': 'test-channel-1'
            },
            {
                'title': 'Breaking News Special',
                'start_time': (now + timedelta(hours=3)).isoformat(),
                'end_time': (now + timedelta(hours=4)).isoformat(),
                'tvg_id': 'test-channel-1'
            }
        ]
    
    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_udi_patcher.stop()
        # Clean up the temp directory
        import shutil
        if os.path.exists(self.test_config_dir):
            shutil.rmtree(self.test_config_dir)
        
        # Reset the global singleton
        scheduling_service._scheduling_service = None
    
    def test_no_deadlock_when_creating_rule(self):
        """Test that creating a rule doesn't cause a deadlock."""
        # Mock fetch_epg_grid to return programs
        with patch.object(self.service, 'fetch_epg_grid', return_value=self.mock_programs):
            rule_data = {
                'name': 'Breaking News Alert',
                'channel_id': 1,
                'regex_pattern': '^Breaking News',
                'minutes_before': 5
            }
            
            # This should complete without hanging
            rule = self.service.create_auto_create_rule(rule_data)
            
            # Give the background thread time to complete
            time.sleep(0.5)
            
            self.assertIsNotNone(rule['id'])
            self.assertEqual(rule['name'], 'Breaking News Alert')
    
    def test_matching_creates_events_synchronously(self):
        """Test that events are created when matching is called directly."""
        # Pre-populate EPG cache and create a rule
        with self.service._lock:
            self.service._epg_cache['test-channel-1'] = {
                'time': datetime.now(),
                'programs': self.mock_programs.copy()
            }
            self.service._auto_create_rules = [{
                'id': 'test-rule-1',
                'name': 'Breaking News Alert',
                'channel_id': 1,
                'tvg_id': 'test-channel-1',
                'regex_pattern': '^Breaking News',
                'minutes_before': 5
            }]
        
        # Directly call match_programs_to_rules
        result = self.service.match_programs_to_rules()
        
        # Check that events were created
        events = self.service.get_scheduled_events()
        
        # Should have created 2 events (for the 2 "Breaking News" programs)
        self.assertEqual(len(events), 2)
        self.assertEqual(result['created'], 2)
        self.assertIn('Breaking News', events[0]['program_title'])
        self.assertIn('Breaking News', events[1]['program_title'])

    def test_match_programs_fetches_all_paginated_epg_pages(self):
        """Auto-create must inspect every EPG page, not only page one."""
        now = datetime.now(timezone.utc)
        first_page = {
            'results': [{
                'title': 'Coming up Tonight',
                'start_time': (now + timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=2)).isoformat(),
                'tvg_id': 'test-channel-1',
            }],
            'next': 'http://test.local/api/epg/programs/?page=2',
        }
        second_page = {
            'results': [
                {
                    'title': 'Live: MLB',
                    'start_time': (now + timedelta(hours=2)).isoformat(),
                    'end_time': (now + timedelta(hours=5)).isoformat(),
                    'tvg_id': 'test-channel-1',
                },
                {
                    'title': 'Live: MLB',
                    'start_time': (now + timedelta(hours=6)).isoformat(),
                    'end_time': (now + timedelta(hours=9)).isoformat(),
                    'tvg_id': 'test-channel-1',
                },
            ],
            'next': None,
        }

        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'mlb-rule',
                'name': 'MLB Auto Create',
                'channel_id': 1,
                'regex_pattern': '^Live: MLB',
                'minutes_before': 0,
            }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url') as mock_fetch, \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_fetch.side_effect = [first_page, second_page, {'data': []}]
            mock_post.return_value.json.return_value = []
            result = self.service.match_programs_to_rules(force_refresh=True)

        self.assertEqual(result['created'], 2)
        self.assertEqual(mock_fetch.call_count, 3)
        self.assertEqual(
            [event['program_title'] for event in self.service.get_scheduled_events()],
            ['Live: MLB', 'Live: MLB'],
        )

    def test_match_programs_uses_effective_epg_tvg_id(self):
        """Auto-create should follow Dispatcharr's effective EPG identity."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Washington Nationals',
            'tvg_id': 'BossSports.MLB_Teams.washingtonnationals',
            'effective_tvg_id': 'WashingtonNationals.mlb',
            'logo_id': None,
        }
        programs = [{
            'title': 'Live: MLB',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'tvg_id': 'WashingtonNationals.mlb',
        }]

        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'mlb-rule',
                'name': 'MLB Auto Create',
                'channel_id': 1,
                'regex_pattern': '^Live: MLB',
                'minutes_before': 0,
            }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.match_programs_to_rules(force_refresh=True)

        events = self.service.get_scheduled_events()
        self.assertEqual(result['created'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['program_title'], 'Live: MLB')
        self.assertEqual(events[0]['tvg_id'], 'WashingtonNationals.mlb')

    def test_match_programs_uses_program_title_alias_and_channel_id_identifier(self):
        """Auto-create should match visible EPG titles even when Dispatcharr omits tvg_id/title."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Boston Celtics',
            'tvg_id': 'BossSports.NBA_Teams.bostonceltics',
            'logo_id': None,
        }
        programs = [{
            'program_title': 'Live: NBA',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'channel_id': 1,
        }]

        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'nba-rule',
                'name': 'NBA Auto Create',
                'channel_id': 1,
                'regex_pattern': '^Live: NBA',
                'minutes_before': 0,
            }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.match_programs_to_rules(force_refresh=True)

        events = self.service.get_scheduled_events()
        self.assertEqual(result['created'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['program_title'], 'Live: NBA')
        self.assertEqual(events[0]['tvg_id'], 'BossSports.NBA_Teams.bostonceltics')

    def test_match_programs_matches_visible_subtitle_and_channel_uuid(self):
        """Auto-create should follow TV Guide channel UUIDs and visible EPG text fields."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'San Francisco Giants',
            'tvg_id': 'BossSports.MLB_Teams.sanfranciscogiants',
            'uuid': 'channel-uuid-1',
            'logo_id': None,
        }
        programs = [{
            'title': 'San Francisco Giants at Los Angeles Dodgers',
            'sub_title': 'Live: MLB',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'channel_uuid': 'channel-uuid-1',
        }]

        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'mlb-rule',
                'name': 'MLB Auto Create',
                'channel_id': 1,
                'regex_pattern': '^Live: MLB',
                'minutes_before': 0,
            }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.match_programs_to_rules(force_refresh=True)

        events = self.service.get_scheduled_events()
        self.assertEqual(result['created'], 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['program_title'], 'San Francisco Giants at Los Angeles Dodgers')
        self.assertEqual(events[0]['tvg_id'], 'BossSports.MLB_Teams.sanfranciscogiants')

    def test_match_programs_merges_epg_grid_when_programs_endpoint_lacks_channel_match(self):
        """Auto-create should use Dispatcharr TV Guide grid data, not only raw programs."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Chicago White Sox',
            'tvg_id': 'BossSports.MLB_Teams.whitesox',
            'effective_tvg_id': 'whitesox-effective',
            'logo_id': None,
        }

        programs_payload = [{
            'title': 'Pregame',
            'start_time': (now + timedelta(minutes=10)).isoformat(),
            'end_time': (now + timedelta(hours=1)).isoformat(),
            'tvg_id': 'unrelated',
        }]
        grid_payload = {'data': [{
            'title': 'Live: MLB',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'tvg_id': 'whitesox-effective',
        }]}

        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'mlb-rule',
                'name': 'MLB Auto Create',
                'channel_id': 1,
                'regex_pattern': '^Live: MLB',
                'minutes_before': 0,
            }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url') as mock_fetch, \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_fetch.side_effect = [programs_payload, grid_payload]
            mock_post.return_value.json.return_value = []
            result = self.service.match_programs_to_rules(force_refresh=True)

        events = self.service.get_scheduled_events()
        self.assertEqual(result['created'], 1)
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertEqual(events[0]['program_title'], 'Live: MLB')

    def test_regex_preview_uses_effective_epg_tvg_id(self):
        """The test popup should use the same effective EPG identity as matching."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Washington Nationals',
            'tvg_id': 'BossSports.MLB_Teams.washingtonnationals',
            'effective_tvg_id': 'WashingtonNationals.mlb',
            'logo_id': None,
        }
        programs = [{
            'title': 'Live: MLB',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'tvg_id': 'WashingtonNationals.mlb',
        }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.test_regex_against_epg_for_rule(
                channel_ids=[1],
                regex_pattern='^Live: MLB',
            )

        self.assertEqual(result['matches'], 1)
        self.assertEqual(result['channels_with_matches'], 1)
        self.assertEqual(result['channels_without_programs'], [])
        self.assertEqual(result['channels_without_matches'], [])

    def test_regex_preview_uses_program_title_alias_and_channel_id_identifier(self):
        """The test popup should match the same visible EPG titles as auto-create."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Boston Celtics',
            'tvg_id': 'BossSports.NBA_Teams.bostonceltics',
            'logo_id': None,
        }
        programs = [{
            'program_title': 'Live: NBA',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'channel_id': 1,
        }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.test_regex_against_epg_for_rule(
                channel_ids=[1],
                regex_pattern='^Live: NBA',
                force_refresh=True,
            )

        self.assertEqual(result['matches'], 1)
        self.assertEqual(result['channels_with_matches'], 1)
        self.assertEqual(result['channels_without_programs'], [])
        self.assertEqual(result['channels_without_matches'], [])
        self.assertEqual(result['programs'][0]['title'], 'Live: NBA')

    def test_regex_preview_reports_visible_fields_for_unmatched_channels(self):
        """The popup should expose which EPG fields were tested when a channel misses."""
        now = datetime.now(timezone.utc)
        self.mock_udi.return_value.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Boston Red Sox',
            'tvg_id': 'redsox',
            'uuid': 'redsox-uuid',
            'logo_id': None,
        }
        programs = [{
            'title': 'Boston Red Sox at New York Yankees',
            'sub_title': 'Pregame Baseball',
            'description': 'Coming up Tonight',
            'start_time': (now + timedelta(hours=1)).isoformat(),
            'end_time': (now + timedelta(hours=4)).isoformat(),
            'channel_uuid': 'redsox-uuid',
        }]

        with patch('apps.automation.scheduling_service.fetch_data_from_url', return_value=programs), \
                patch('apps.automation.scheduling_service.post_request') as mock_post:
            mock_post.return_value.json.return_value = []
            result = self.service.test_regex_against_epg_for_rule(
                channel_ids=[1],
                regex_pattern='^Live: MLB',
                force_refresh=True,
            )

        self.assertEqual(result['matches'], 0)
        self.assertEqual(result['channels_without_matches'][0]['sample_titles'], [
            'Boston Red Sox at New York Yankees',
        ])
        self.assertEqual(
            result['channels_without_matches'][0]['sample_fields'][0]['fields'][:3],
            ['title', 'sub_title', 'description'],
        )
    
    def test_match_programs_completes_without_deadlock(self):
        """Test that match_programs_to_rules completes without deadlock."""
        # Pre-populate EPG cache and rules
        with self.service._lock:
            self.service._epg_cache['test-channel-1'] = {
                'time': datetime.now(),
                'programs': self.mock_programs.copy()
            }
            self.service._auto_create_rules = [{
                'id': 'test-rule-1',
                'name': 'Test Rule',
                'channel_id': 1,
                'tvg_id': 'test-channel-1',
                'regex_pattern': '^Breaking News',
                'minutes_before': 5
            }]
        
        # This should complete without hanging
        result = self.service.match_programs_to_rules()
        
        self.assertEqual(result['created'], 2)  # Should create 2 events
        self.assertEqual(result['updated'], 0)
        
        # Verify events were created
        events = self.service.get_scheduled_events()
        self.assertEqual(len(events), 2)
    
    def test_concurrent_access_no_deadlock(self):
        """Test that concurrent access to the service doesn't cause deadlock."""
        with self.service._lock:
            self.service._epg_cache['test-channel-1'] = {
                'time': datetime.now(),
                'programs': self.mock_programs.copy()
            }
        
        errors = []
        
        def match_programs():
            try:
                self.service.match_programs_to_rules()
            except Exception as e:
                errors.append(e)
        
        # Start multiple threads
        threads = []
        for _ in range(3):
            t1 = threading.Thread(target=match_programs)
            t1.start()
            threads.append(t1)
        
        # Wait for all threads with timeout
        for t in threads:
            t.join(timeout=2.0)
            if t.is_alive():
                self.fail("Thread deadlocked and didn't complete")
        
        # Check no errors occurred
        if errors:
            self.fail(f"Errors occurred: {errors}")

    def test_prevents_cross_channel_leakage(self):
        """Test that programs from other channels (returned by API) are not leaked into the current channel."""
        # Define 2 channels
        channel1 = {'id': 1, 'name': 'Channel 1', 'tvg_id': 'tvg-1'}
        channel2 = {'id': 2, 'name': 'Channel 2', 'tvg_id': 'tvg-2'}
        
        # Mock UDI to return these channels
        def get_channel(cid):
            if cid == 1: return channel1
            if cid == 2: return channel2
            return None
        self.mock_udi.return_value.get_channel_by_id.side_effect = get_channel
        
        # Mixed API response (simulating Dispatcharr returning extra data)
        now = datetime.now(timezone.utc)
        mixed_programs = [
            {
                'title': 'Correct Match Channel 1',
                'start_time': (now + timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=2)).isoformat(),
                'tvg_id': 'tvg-1'
            },
            {
                'title': 'Foreign Match Channel 2',
                'start_time': (now + timedelta(hours=1)).isoformat(),
                'end_time': (now + timedelta(hours=2)).isoformat(),
                'tvg_id': 'tvg-2'
            }
        ]
        
        # Create a rule specifically for Channel 1
        with self.service._lock:
            self.service._auto_create_rules = [{
                'id': 'rule-1',
                'name': 'Match All',
                'channel_id': 1,
                'regex_pattern': 'Match',
                'minutes_before': 0
            }]
            
        # Mock the API fetch to return the mixed list
        with patch('scheduling_service.fetch_data_from_url', return_value=mixed_programs):
            # Run matching
            self.service.match_programs_to_rules(force_refresh=True)
            
        # Verify results
        events = self.service.get_scheduled_events()
        
        # IMPORTANT: Should only have ONE event, and it MUST be for Channel 1
        self.assertEqual(len(events), 1, "Should have only created 1 event despite multiple API matches")
        self.assertEqual(events[0]['channel_id'], 1)
        self.assertEqual(events[0]['program_title'], 'Correct Match Channel 1')
        self.assertNotEqual(events[0]['program_title'], 'Foreign Match Channel 2')


if __name__ == '__main__':
    unittest.main()

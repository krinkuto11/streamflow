#!/usr/bin/env python3
"""
Tests for current_viewers tracking in UDI and stream limit calculations.

This test suite verifies that:
1. UDI properly stores and retrieves current_viewers from streams
2. Active stream counting works correctly for accounts and profiles
3. Stream limits consider active viewers when checking streams
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.manager import UDIManager
from apps.stream.concurrent_stream_limiter import AccountStreamLimiter


class TestCurrentViewersTracking(unittest.TestCase):
    """Test current_viewers tracking in UDI."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock UDI Manager with test data
        self.udi = UDIManager()
        self.udi._initialized = True
        
        # Test M3U accounts
        self.udi._m3u_accounts_cache = [
            {
                'id': 1,
                'name': 'Test Account 1',
                'max_streams': 2,
                'profiles': [
                    {'id': 101, 'name': 'Profile 1', 'max_streams': 2}
                ]
            },
            {
                'id': 2,
                'name': 'Test Account 2',
                'max_streams': 5,
                'profiles': [
                    {'id': 201, 'name': 'Profile 2', 'max_streams': 5}
                ]
            }
        ]
        
        # Test channels (needed for proxy status to work)
        self.udi._channels_cache = [
            {'id': 100, 'name': 'Channel 100', 'streams': [2]},  # Stream 2 from Account 1
            {'id': 101, 'name': 'Channel 101', 'streams': [3]},  # Stream 3 from Account 1
            {'id': 102, 'name': 'Channel 102', 'streams': [4]},  # Stream 4 from Account 2
            {'id': 103, 'name': 'Channel 103', 'streams': [6]},  # Stream 6 from Account 2
        ]
        
        # Test streams with current_viewers
        self.udi._streams_cache = [
            # Account 1 streams
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1, 'current_viewers': 0},
            {'id': 2, 'name': 'Stream 2', 'm3u_account': 1, 'current_viewers': 1},
            {'id': 3, 'name': 'Stream 3', 'm3u_account': 1, 'current_viewers': 2},
            # Account 2 streams
            {'id': 4, 'name': 'Stream 4', 'm3u_account': 2, 'current_viewers': 3},
            {'id': 5, 'name': 'Stream 5', 'm3u_account': 2, 'current_viewers': 0},
            {'id': 6, 'name': 'Stream 6', 'm3u_account': 2, 'current_viewers': 1},
        ]
        
        self.udi._build_indexes()
    
    def test_get_active_streams_for_account(self):
        """Test counting active streams for an account."""
        # Mock proxy status showing channels 100, 101 (account 1), and 102, 103 (account 2) are active
        mock_proxy_status = {
            '100': {'channel_id': '100', 'state': 'active', 'm3u_profile_id': 101},  # Account 1
            '101': {'channel_id': '101', 'state': 'active', 'm3u_profile_id': 101},  # Account 1
            '102': {'channel_id': '102', 'state': 'active', 'm3u_profile_id': 201},  # Account 2
            '103': {'channel_id': '103', 'state': 'active', 'm3u_profile_id': 201},  # Account 2
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            # Account 1 has 2 active streams (channels 100 and 101)
            active_count = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count, 2, "Account 1 should have 2 active streams")
            
            # Account 2 has 2 active streams (channels 102 and 103)
            active_count = self.udi.get_active_streams_for_account(2)
            self.assertEqual(active_count, 2, "Account 2 should have 2 active streams")
    
    def test_get_total_viewers_for_account(self):
        """Test summing total viewers for an account."""
        # Account 1: 0 + 1 + 2 = 3 total viewers
        total_viewers = self.udi.get_total_viewers_for_account(1)
        self.assertEqual(total_viewers, 3, "Account 1 should have 3 total viewers")
        
        # Account 2: 3 + 0 + 1 = 4 total viewers
        total_viewers = self.udi.get_total_viewers_for_account(2)
        self.assertEqual(total_viewers, 4, "Account 2 should have 4 total viewers")
    
    def test_get_active_streams_for_profile(self):
        """Test counting active streams for a profile."""
        # Mock proxy status showing channels 100, 101 (account 1), and 102, 103 (account 2) are active
        mock_proxy_status = {
            '100': {'channel_id': '100', 'state': 'active', 'm3u_profile_id': 101},  # Account 1
            '101': {'channel_id': '101', 'state': 'active', 'm3u_profile_id': 101},  # Account 1
            '102': {'channel_id': '102', 'state': 'active', 'm3u_profile_id': 201},  # Account 2
            '103': {'channel_id': '103', 'state': 'active', 'm3u_profile_id': 201},  # Account 2
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            # Profile 101 belongs to account 1, which has 2 active streams
            active_count = self.udi.get_active_streams_for_profile(101)
            self.assertEqual(active_count, 2, "Profile 101 should have 2 active streams")
            
            # Profile 201 belongs to account 2, which has 2 active streams
            active_count = self.udi.get_active_streams_for_profile(201)
            self.assertEqual(active_count, 2, "Profile 201 should have 2 active streams")
    
    def test_get_total_viewers_for_profile(self):
        """Test summing total viewers for a profile."""
        # Profile 101 belongs to account 1: 3 total viewers
        total_viewers = self.udi.get_total_viewers_for_profile(101)
        self.assertEqual(total_viewers, 3, "Profile 101 should have 3 total viewers")
        
        # Profile 201 belongs to account 2: 4 total viewers
        total_viewers = self.udi.get_total_viewers_for_profile(201)
        self.assertEqual(total_viewers, 4, "Profile 201 should have 4 total viewers")
    
    def test_nonexistent_account(self):
        """Test handling of nonexistent account."""
        self.udi.fetcher.fetch_proxy_status = Mock(return_value={})
        active_count = self.udi.get_active_streams_for_account(999)
        self.assertEqual(active_count, 0, "Nonexistent account should return 0 active streams")
        
        total_viewers = self.udi.get_total_viewers_for_account(999)
        self.assertEqual(total_viewers, 0, "Nonexistent account should return 0 viewers")
    
    def test_nonexistent_profile(self):
        """Test handling of nonexistent profile."""
        active_count = self.udi.get_active_streams_for_profile(999)
        self.assertEqual(active_count, 0, "Nonexistent profile should return 0 active streams")
        
        total_viewers = self.udi.get_total_viewers_for_profile(999)
        self.assertEqual(total_viewers, 0, "Nonexistent profile should return 0 viewers")



class TestStreamLimiterWithCurrentViewers(unittest.TestCase):
    """Test stream limiter considers current_viewers."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock UDI Manager
        self.mock_udi = Mock()
        
        # Create limiter with UDI
        self.limiter = AccountStreamLimiter(udi_manager=self.mock_udi)
    
    def test_get_available_slots_with_active_viewers(self):
        """Test available slots calculation with active viewers."""
        # Set account limit to 5
        self.limiter.set_account_limit(1, 5)
        
        # Mock UDI to return 3 active streams
        self.mock_udi.get_active_streams_for_account.return_value = 3
        
        # Should have 2 available slots (5 - 3)
        available = self.limiter.get_available_slots(1)
        self.assertEqual(available, 2, "Should have 2 available slots")
    
    def test_get_available_slots_at_limit(self):
        """Test available slots when at limit."""
        # Set account limit to 3
        self.limiter.set_account_limit(1, 3)
        
        # Mock UDI to return 3 active streams (at limit)
        self.mock_udi.get_active_streams_for_account.return_value = 3
        
        # Should have 0 available slots
        available = self.limiter.get_available_slots(1)
        self.assertEqual(available, 0, "Should have 0 available slots")
    
    def test_get_available_slots_over_limit(self):
        """Test available slots when over limit (edge case)."""
        # Set account limit to 2
        self.limiter.set_account_limit(1, 2)
        
        # Mock UDI to return 5 active streams (over limit)
        self.mock_udi.get_active_streams_for_account.return_value = 5
        
        # Should have 0 available slots (not negative)
        available = self.limiter.get_available_slots(1)
        self.assertEqual(available, 0, "Should have 0 available slots (capped at 0)")
    
    def test_get_available_slots_unlimited(self):
        """Test available slots for unlimited account."""
        # Set account limit to 0 (unlimited)
        self.limiter.set_account_limit(1, 0)
        
        # Mock UDI to return any number of active streams
        self.mock_udi.get_active_streams_for_account.return_value = 100
        
        # Should return -1 for unlimited
        available = self.limiter.get_available_slots(1)
        self.assertEqual(available, -1, "Should return -1 for unlimited")
    
    def test_acquire_blocks_when_at_limit(self):
        """Test that acquire blocks when at limit due to active viewers."""
        # Set account limit to 2
        self.limiter.set_account_limit(1, 2)
        
        # Mock UDI to return 2 active streams (at limit)
        self.mock_udi.get_active_streams_for_account.return_value = 2
        
        # Should not be able to acquire
        acquired, reason = self.limiter.acquire(1, timeout=0.1)
        self.assertFalse(acquired, "Should not acquire when at limit")
        self.assertEqual(reason, 'active_viewers', "Should fail due to active viewers")
    
    def test_acquire_succeeds_when_below_limit(self):
        """Test that acquire succeeds when below limit."""
        # Set account limit to 5
        self.limiter.set_account_limit(1, 5)
        
        # Mock UDI to return 2 active streams
        self.mock_udi.get_active_streams_for_account.return_value = 2
        
        # Should be able to acquire
        acquired, reason = self.limiter.acquire(1, timeout=0.1)
        self.assertTrue(acquired, "Should acquire when below limit")
        self.assertEqual(reason, 'acquired', "Should succeed with 'acquired' reason")
        
        # Clean up
        if acquired:
            self.limiter.release(1)
    
    def test_acquire_handles_udi_error(self):
        """Test that acquire handles UDI errors gracefully."""
        # Set account limit to 2
        self.limiter.set_account_limit(1, 2)
        
        # Mock UDI to raise an error
        self.mock_udi.get_active_streams_for_account.side_effect = Exception("UDI error")
        
        # Unknown live provider usage must not be treated as zero.
        acquired, reason = self.limiter.acquire(1, timeout=0.1)
        self.assertFalse(acquired)
        self.assertEqual(reason, 'provider_usage_unavailable')
        
        # Clean up
        if acquired:
            self.limiter.release(1)
    
    def test_cached_stats_returned_when_quota_consumed(self):
        """Test that cached stats are returned when quota is consumed by active viewers."""
        from apps.stream.concurrent_stream_limiter import SmartStreamScheduler
        
        # Create a fresh UDI for this test with mock
        mock_udi = Mock()
        
        # Create limiter with the mock UDI
        limiter = AccountStreamLimiter(udi_manager=mock_udi)
        
        # Set account limit to 2
        limiter.set_account_limit(1, 2)
        
        # Mock UDI to return 2 active streams (at limit)
        mock_udi.get_active_streams_for_account.return_value = 2
        
        # Mock get_stream_by_id to return cached stats
        mock_udi.get_stream_by_id.return_value = {
            'id': 1,
            'name': 'Test Stream',
            'stream_stats': {
                'resolution': '1920x1080',
                'bitrate_kbps': 5000,
                'fps': 30,
                'video_codec': 'h264',
                'audio_codec': 'aac'
            }
        }
        
        # Create scheduler with the limiter
        scheduler = SmartStreamScheduler(limiter, global_limit=10)
        
        # Mock check function (should not be called for cached results)
        check_func = Mock(return_value={'status': 'OK'})
        
        # Test stream
        streams = [{'id': 1, 'name': 'Test Stream', 'url': 'http://test.com', 'm3u_account': 1}]
        
        # Run check
        results = scheduler.check_streams_with_limits(
            streams,
            check_func,
            provider_wait_timeout=0.1,
        )
        
        # Should have 1 result with cached stats
        self.assertEqual(len(results), 1, "Should have 1 result")
        self.assertTrue(results[0].get('cached'), "Result should be marked as cached")
        self.assertEqual(results[0].get('resolution'), '1920x1080', "Should have cached resolution")
        self.assertEqual(results[0].get('skipped_reason'), 'quota_consumed_by_active_viewers')
        
        # Check function should not have been called
        check_func.assert_not_called()


if __name__ == '__main__':
    unittest.main()

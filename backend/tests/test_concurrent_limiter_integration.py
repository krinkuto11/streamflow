#!/usr/bin/env python3
"""
Integration test for concurrent stream limiter with stream_checker_service.

This test validates the complete integration of the concurrent stream limiter
with the stream checking service.
"""

import unittest
import pytest
from unittest.mock import Mock, patch, MagicMock
import sys
import os
import json

pytestmark = pytest.mark.integration

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConcurrentLimiterIntegration(unittest.TestCase):
    """Integration test for concurrent stream limiter."""
    
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.analyze_stream')
    @patch('stream_checker_service.update_channel_streams')
    @patch('stream_checker_service._get_base_url')
    def test_check_channel_respects_account_limits(
        self, 
        mock_base_url,
        mock_update_streams,
        mock_analyze,
        mock_fetch_streams,
        mock_udi
    ):
        """Test that _check_channel_concurrent respects account limits."""
        
        # Setup mocks
        mock_base_url.return_value = 'http://localhost:9191'
        
        # Mock UDI manager
        udi_mock = MagicMock()
        udi_mock.get_channel_by_id.return_value = {
            'id': 1,
            'name': 'Test Channel',
            'streams': [1, 2, 3, 4, 5]
        }
        
        # Mock M3U accounts with different limits
        udi_mock.get_m3u_accounts.return_value = [
            {'id': 1, 'name': 'Account A', 'max_streams': 1},  # Max 1 concurrent
            {'id': 2, 'name': 'Account B', 'max_streams': 2},  # Max 2 concurrent
        ]
        
        udi_mock.get_stream_by_id.return_value = None  # No cached data
        udi_mock.refresh_channel_by_id.return_value = True
        
        mock_udi.return_value = udi_mock
        
        # Mock streams from different accounts
        mock_fetch_streams.return_value = [
            {'id': 1, 'name': 'Stream A1', 'url': 'http://a.com/1', 'm3u_account': 1},
            {'id': 2, 'name': 'Stream A2', 'url': 'http://a.com/2', 'm3u_account': 1},
            {'id': 3, 'name': 'Stream B1', 'url': 'http://b.com/1', 'm3u_account': 2},
            {'id': 4, 'name': 'Stream B2', 'url': 'http://b.com/2', 'm3u_account': 2},
            {'id': 5, 'name': 'Stream B3', 'url': 'http://b.com/3', 'm3u_account': 2},
        ]
        
        # Mock stream analysis
        def analyze_side_effect(**kwargs):
            return {
                'stream_id': kwargs['stream_id'],
                'stream_name': kwargs['stream_name'],
                'stream_url': kwargs['stream_url'],
                'resolution': '1920x1080',
                'fps': 30,
                'bitrate_kbps': 5000,
                'video_codec': 'h264',
                'audio_codec': 'aac',
                'status': 'OK'
            }
        
        mock_analyze.side_effect = analyze_side_effect
        
        # Create service and check channel
        from apps.stream.stream_checker_service import StreamCheckerService
        service = StreamCheckerService()
        
        # Enable concurrent checking
        service.config.config['concurrent_streams'] = {
            'enabled': True,
            'global_limit': 10,
            'stagger_delay': 0.0
        }
        
        # Clear any existing tracking
        service.update_tracker.updates = {'channels': {}, 'last_global_check': None}
        
        # Check the channel
        service._check_channel_concurrent(1)
        
        # Verify analyze_stream was called for all streams
        self.assertEqual(mock_analyze.call_count, 5)
        
        # Verify update_channel_streams was called
        self.assertEqual(mock_update_streams.call_count, 1)
        
        # Verify channel was marked as checked
        channel_info = service.update_tracker.updates['channels'].get('1')
        self.assertIsNotNone(channel_info)
        self.assertFalse(channel_info.get('needs_check', True))
    
    def test_initialization_loads_account_limits(self):
        """Test that account limits are properly initialized from UDI."""
        from apps.stream.concurrent_stream_limiter import get_account_limiter, initialize_account_limits
        
        limiter = get_account_limiter()
        limiter.clear()
        
        # Mock accounts
        accounts = [
            {'id': 1, 'name': 'Account A', 'max_streams': 1},
            {'id': 2, 'name': 'Account B', 'max_streams': 2},
            {'id': 3, 'name': 'Account C', 'max_streams': 0},  # Unlimited
        ]
        
        initialize_account_limits(accounts)
        
        # Verify limits were set
        self.assertEqual(limiter.get_account_limit(1), 1)
        self.assertEqual(limiter.get_account_limit(2), 2)
        self.assertEqual(limiter.get_account_limit(3), 0)
        
        # Verify semaphores work correctly
        # Account 1: max 1
        self.assertTrue(limiter.acquire(1, timeout=0.1))
        self.assertFalse(limiter.acquire(1, timeout=0.1))
        limiter.release(1)
        
        # Account 2: max 2
        self.assertTrue(limiter.acquire(2, timeout=0.1))
        self.assertTrue(limiter.acquire(2, timeout=0.1))
        self.assertFalse(limiter.acquire(2, timeout=0.1))
        limiter.release(2)
        limiter.release(2)
        
        # Account 3: unlimited
        for _ in range(10):
            self.assertTrue(limiter.acquire(3, timeout=0.1))


if __name__ == '__main__':
    unittest.main()

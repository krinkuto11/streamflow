#!/usr/bin/env python3
"""
Test to verify that single channel check correctly counts dead streams.

This test validates that the fix for dead stream counting is working properly.
The issue was that dead streams were removed from the channel before counting them,
resulting in a dead_count of 0 in the changelog.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSingleChannelDeadCount(unittest.TestCase):
    """Test that single channel check correctly counts dead streams."""
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_counts_dead_streams_correctly(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that check_single_channel correctly counts dead streams that are removed."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Mock config
        mock_config = Mock()
        mock_config.get = Mock(side_effect=lambda key, default=None: default)
        mock_config_class.return_value = mock_config
        
        # Setup mocks
        mock_udi_instance = Mock()
        mock_udi.return_value = mock_udi_instance
        
        # Mock channel data
        mock_channel = {
            'id': 16,
            'name': 'Test Channel',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # First call returns streams with dead ones (before check)
        # Second call returns only live streams (after check removes dead ones)
        mock_streams_before = [
            {'id': 1, 'name': 'Live Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1920x1080', 'source_fps': '30'}},
            {'id': 2, 'name': 'Dead Stream 1', 'url': 'http://example.com/2', 'm3u_account': 1,
             'stream_stats': {'status': 'dead'}},
            {'id': 3, 'name': 'Live Stream 2', 'url': 'http://example.com/3', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1280x720', 'source_fps': '25'}},
            {'id': 4, 'name': 'Dead Stream 2', 'url': 'http://example.com/4', 'm3u_account': 1,
             'stream_stats': {'status': 'dead'}}
        ]
        
        mock_streams_after = [
            {'id': 1, 'name': 'Live Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1920x1080', 'source_fps': '30'}},
            {'id': 3, 'name': 'Live Stream 2', 'url': 'http://example.com/3', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1280x720', 'source_fps': '25'}}
        ]
        
        # First two calls are for getting M3U accounts (step 1)
        # Third call is for final statistics gathering after check
        mock_fetch_streams.side_effect = [
            mock_streams_before,  # First call in step 1
            mock_streams_after    # Second call after _check_channel
        ]
        
        # Mock UDI refresh methods
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams_before)
        
        # Mock AutomatedStreamManager
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Mock _check_channel to return dead stream stats
        # Simulate that 2 dead streams were removed during the check
        mock_check_result = {
            'dead_streams_count': 2,
            'revived_streams_count': 0
        }
        service._check_channel = Mock(return_value=mock_check_result)
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify the result includes correct statistics
        self.assertTrue(result['success'], "Check should succeed")
        
        # The key assertion: dead_streams should be 2, not 0
        check_stats = result['stats']
        self.assertEqual(check_stats['dead_streams'], 2,
                        "Should correctly count 2 dead streams that were removed")
        
        # Total streams after removal should be 2
        self.assertEqual(check_stats['total_streams'], 2,
                        "Should have 2 streams remaining after removing dead ones")
        
        # Verify _check_channel was called
        service._check_channel.assert_called_once_with(
            16,
            skip_batch_changelog=True,
            run_mode='single_channel_check',
            is_single_channel_check=True,
        )
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_handles_no_dead_streams(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that check_single_channel correctly handles case with no dead streams."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Mock config
        mock_config = Mock()
        mock_config.get = Mock(side_effect=lambda key, default=None: default)
        mock_config_class.return_value = mock_config
        
        # Setup mocks
        mock_udi_instance = Mock()
        mock_udi.return_value = mock_udi_instance
        
        mock_channel = {
            'id': 16,
            'name': 'Test Channel',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # All streams are alive
        mock_streams = [
            {'id': 1, 'name': 'Live Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1920x1080', 'source_fps': '30'}},
            {'id': 2, 'name': 'Live Stream 2', 'url': 'http://example.com/2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok', 'resolution': '1280x720', 'source_fps': '25'}}
        ]
        
        mock_fetch_streams.side_effect = [mock_streams, mock_streams]
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        service = StreamCheckerService()
        
        # Mock _check_channel to return no dead streams
        mock_check_result = {
            'dead_streams_count': 0,
            'revived_streams_count': 0
        }
        service._check_channel = Mock(return_value=mock_check_result)
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify the result
        self.assertTrue(result['success'])
        check_stats = result['stats']
        self.assertEqual(check_stats['dead_streams'], 0, "Should have 0 dead streams")
        self.assertEqual(check_stats['total_streams'], 2, "Should have 2 total streams")


if __name__ == '__main__':
    unittest.main()

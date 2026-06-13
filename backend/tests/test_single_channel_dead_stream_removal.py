#!/usr/bin/env python3
"""
Test to verify that single channel check removes dead streams from tracker
before refreshing playlists and rematching streams.

This ensures that previously dead streams can be re-added and re-checked
during the single channel check process.
"""

import unittest
from unittest.mock import Mock, patch, call
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSingleChannelDeadStreamRemoval(unittest.TestCase):
    """Test that single channel check removes dead streams before refresh."""
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_removes_dead_streams_before_refresh(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that dead streams are removed from tracker before playlist refresh."""
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
        
        # Mock streams - these are the current streams in the channel
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/stream2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://example.com/stream3', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}}
        ]
        
        # First call: get current streams (Step 1)
        # Second call: get stats after check (after all steps complete)
        mock_fetch_streams.side_effect = [mock_streams, mock_streams]
        
        # Mock UDI refresh methods and get_streams for dead stream clearing
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        # Mock AutomatedStreamManager
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Pre-mark some streams as dead in the tracker
        # These streams are from the channel we're about to check
        service.dead_streams_tracker.mark_as_dead('http://example.com/stream1', 1, 'Stream 1', channel_id=16)
        service.dead_streams_tracker.mark_as_dead('http://example.com/stream2', 2, 'Stream 2', channel_id=16)
        
        # Verify they are marked as dead
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/stream1'))
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/stream2'))
        
        # Mock _check_channel to avoid actual checking
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify the result
        self.assertTrue(result['success'], "Check should succeed")
        
        # The key assertion: dead streams should have been removed from tracker
        # before playlist refresh and rematching
        self.assertFalse(service.dead_streams_tracker.is_dead('http://example.com/stream1'),
            "Stream 1 should have been removed from dead tracker before refresh")
        self.assertFalse(service.dead_streams_tracker.is_dead('http://example.com/stream2'),
            "Stream 2 should have been removed from dead tracker before refresh")
        
        # Verify that playlist refresh was called (after dead stream removal)
        self.assertTrue(mock_refresh.called, "Playlist refresh should be called")
        
        # Verify that stream matching was called (after dead stream removal)
        mock_automation_instance.discover_and_assign_streams.assert_called_once()
        
        # Verify _check_channel was called (after everything else)
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
    def test_single_channel_only_removes_channel_dead_streams(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that only dead streams for the specific channel are removed."""
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
        
        # Mock streams for channel 16
        mock_streams_ch16 = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/ch16/stream1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/ch16/stream2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}}
        ]
        
        mock_fetch_streams.side_effect = [mock_streams_ch16, mock_streams_ch16]
        
        # Mock all streams in UDI (including streams from other channels/accounts)
        all_udi_streams = mock_streams_ch16 + [
            {'id': 99, 'name': 'CH99 Stream 1', 'url': 'http://example.com/ch99/stream1', 'm3u_account': 99},
            {'id': 88, 'name': 'CH88 Stream 1', 'url': 'http://example.com/ch88/stream1', 'm3u_account': 88}
        ]
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=all_udi_streams)
        
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Mark dead streams from channel 16
        service.dead_streams_tracker.mark_as_dead('http://example.com/ch16/stream1', 1, 'CH16 Stream 1', channel_id=16)
        
        # Mark dead streams from OTHER channels (should NOT be removed)
        service.dead_streams_tracker.mark_as_dead('http://example.com/ch99/stream1', 99, 'CH99 Stream 1', channel_id=99)
        service.dead_streams_tracker.mark_as_dead('http://example.com/ch88/stream1', 88, 'CH88 Stream 1', channel_id=88)
        
        # Verify all are marked as dead
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch16/stream1'))
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch99/stream1'))
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch88/stream1'))
        
        # Mock _check_channel
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})
        
        # Call check_single_channel for channel 16
        result = service.check_single_channel(channel_id=16)
        
        # Verify the result
        self.assertTrue(result['success'])
        
        # Channel 16's dead stream should be removed
        self.assertFalse(service.dead_streams_tracker.is_dead('http://example.com/ch16/stream1'),
            "Channel 16's dead stream should have been removed")
        
        # Other channels' dead streams should remain
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch99/stream1'),
            "Channel 99's dead stream should remain (not affected)")
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch88/stream1'),
            "Channel 88's dead stream should remain (not affected)")
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_handles_no_dead_streams(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that check proceeds normally when there are no dead streams to remove."""
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
        
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}}
        ]
        
        mock_fetch_streams.side_effect = [mock_streams, mock_streams]
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Don't mark any streams as dead
        
        # Mock _check_channel
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify the result
        self.assertTrue(result['success'], "Check should succeed even with no dead streams to remove")
        
        # All other steps should still execute normally
        mock_refresh.assert_called()
        mock_automation_instance.discover_and_assign_streams.assert_called_once()
        service._check_channel.assert_called_once_with(
            16,
            skip_batch_changelog=True,
            run_mode='single_channel_check',
            is_single_channel_check=True,
        )


if __name__ == '__main__':
    unittest.main()

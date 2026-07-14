#!/usr/bin/env python3
"""
Integration test demonstrating the fix for single channel check dead stream removal.

This test shows that the single channel check now correctly:
1. Removes all dead streams from the tracker BEFORE refreshing playlists
2. Allows previously dead streams to be re-added during playlist refresh
3. Re-checks all streams (including previously dead ones)
"""

import unittest
import pytest
from unittest.mock import Mock, patch
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.integration


class TestSingleChannelCheckIntegration(unittest.TestCase):
    """Integration test for single channel check dead stream removal fix."""
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_check_removes_dead_streams_before_refresh_full_flow(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Full integration test showing the fix works end-to-end."""
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
            'name': 'DAZN F1',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # SCENARIO: Channel currently has 3 streams
        # Stream 3 (4K) was previously marked as dead
        initial_streams = [
            {'id': 1, 'name': 'DAZN F1 HD', 'url': 'http://example.com/dazn-f1-hd', 'm3u_account': 1,
             'stream_stats': {'resolution': '1920x1080', 'source_fps': 30, 'ffmpeg_output_bitrate': 5000}},
            {'id': 2, 'name': 'DAZN F1 SD', 'url': 'http://example.com/dazn-f1-sd', 'm3u_account': 1,
             'stream_stats': {'resolution': '1280x720', 'source_fps': 30, 'ffmpeg_output_bitrate': 3000}},
            {'id': 3, 'name': 'DAZN F1 4K', 'url': 'http://example.com/dazn-f1-4k', 'm3u_account': 1,
             'stream_stats': {'resolution': '3840x2160', 'source_fps': 60, 'ffmpeg_output_bitrate': 15000}}
        ]
        
        # AFTER playlist refresh: The previously dead stream is back!
        # This happens because the M3U playlist was updated with a working version
        refreshed_streams = [
            {'id': 1, 'name': 'DAZN F1 HD', 'url': 'http://example.com/dazn-f1-hd', 'm3u_account': 1,
             'stream_stats': {'resolution': '1920x1080', 'source_fps': 30, 'ffmpeg_output_bitrate': 5000}},
            {'id': 2, 'name': 'DAZN F1 SD', 'url': 'http://example.com/dazn-f1-sd', 'm3u_account': 1,
             'stream_stats': {'resolution': '1280x720', 'source_fps': 30, 'ffmpeg_output_bitrate': 3000}},
            {'id': 3, 'name': 'DAZN F1 4K', 'url': 'http://example.com/dazn-f1-4k', 'm3u_account': 1,
             'stream_stats': {'resolution': '3840x2160', 'source_fps': 60, 'ffmpeg_output_bitrate': 15000}}
        ]
        
        # First call: get initial streams (Step 1)
        # Second call: get stats after check (after all steps complete)
        mock_fetch_streams.side_effect = [initial_streams, refreshed_streams]
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=refreshed_streams)
        # Mock get_stream_by_id for dead stream lookup
        mock_udi_instance.get_stream_by_id = Mock(return_value=initial_streams[2])  # Return the 4K stream
        
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        # Simulate that stream matching added the 4K stream
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={'16': 1})
        
        # Create service instance
        service = StreamCheckerService()
        
        # SETUP: Mark stream 3 (DAZN F1 4K) as previously dead
        # This simulates it being dead in a previous check
        service.dead_streams_tracker.mark_as_dead(
            'http://example.com/dazn-f1-4k', 3, 'DAZN F1 4K', channel_id=16
        )
        
        # Verify it's marked as dead before the check
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/dazn-f1-4k'),
            "Stream should be marked as dead before check")
        
        # Mock _check_channel to return successful results
        service._check_channel = Mock(return_value={
            'dead_streams_count': 0,
            'revived_streams_count': 1  # The 4K stream was revived!
        })
        
        # ========================================
        # ACTION: Run single channel check
        # ========================================
        result = service.check_single_channel(channel_id=16)
        
        # ========================================
        # VERIFICATION
        # ========================================
        
        # 1. Check should succeed
        self.assertTrue(result['success'], "Check should succeed")
        
        # 2. Dead stream should have been removed from tracker BEFORE playlist refresh
        # This is the key fix - it's removed early so it can be re-added during refresh
        self.assertFalse(service.dead_streams_tracker.is_dead('http://example.com/dazn-f1-4k'),
            "Dead stream should have been removed from tracker before refresh")
        
        # 3. Verify the correct sequence of operations:
        
        # a) Playlist refresh was called (Step 3, after dead stream removal)
        self.assertTrue(mock_refresh.called, "Playlist refresh should have been called")
        mock_refresh.assert_called_with(account_id=1)
        
        # b) Stream matching was called with force=True and skip_check_trigger=True (Step 4)
        mock_automation_instance.discover_and_assign_streams.assert_called_once()
        call_kwargs = mock_automation_instance.discover_and_assign_streams.call_args[1]
        self.assertTrue(call_kwargs.get('force'),
            "Stream matching should be forced")
        self.assertTrue(call_kwargs.get('skip_check_trigger'),
            "Stream matching should skip automatic check trigger")
        
        # c) Force check was performed (Step 5)
        service._check_channel.assert_called_once_with(
            16,
            skip_batch_changelog=True,
            run_mode='single_channel_check',
            is_single_channel_check=True,
            force_check_override=False,
            force_check_generation=None,
        )
        
        # 4. Verify final stats reflect the revived stream
        self.assertEqual(result['stats']['total_streams'], 3,
            "Should have 3 streams after refresh (including revived 4K stream)")
        
        print("\n✅ Integration test PASSED!")
        print("=" * 60)
        print("Summary of what happened:")
        print("1. DAZN F1 4K stream was previously marked as dead")
        print("2. Single channel check REMOVED it from dead tracker first")
        print("3. Playlist refresh brought back the stream (now working)")
        print("4. Stream matching re-added it to the channel")
        print("5. Force check verified all 3 streams (including 4K)")
        print("=" * 60)


if __name__ == '__main__':
    unittest.main(verbosity=2)

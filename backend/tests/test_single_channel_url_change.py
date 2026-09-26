#!/usr/bin/env python3
"""
Test to verify that single channel check removes dead streams by channel_id
even when stream URLs change after playlist refresh.

This test simulates the real-world scenario where:
1. Stream is marked dead with URL A
2. Playlist refresh creates a new stream with URL B
3. Dead stream with URL A should still be cleared because it has the same channel_id
"""

import unittest
from unittest.mock import Mock, patch
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSingleChannelURLChange(unittest.TestCase):
    """Test that single channel check handles URL changes after playlist refresh."""
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_dead_streams_cleared_when_urls_change(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that dead streams are cleared even when URLs change after playlist refresh."""
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
            'id': 31,
            'name': 'Movistar Plus',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # BEFORE refresh: Channel has streams with OLD URLs
        old_streams = [
            {
                'id': 8916,
                'name': 'MOVISTAR PLUS FHD --> NEW ERA',
                'url': 'http://acexy:8080/ace/getstream?id=8c67cdb5ba81976662c3a67984a9545d9cfb0f70',
                'm3u_account': 6,
                'stream_stats': {'status': 'dead'}
            },
            {
                'id': 7063,
                'name': 'MOVISTAR PLUS FHD --> ELCANO',
                'url': 'http://acexy:8080/ace/getstream?id=1ab443f5b4beb6d586f19e8b25b9f9646cf2ab78',
                'm3u_account': 6,
                'stream_stats': {'status': 'dead'}
            }
        ]
        
        # AFTER refresh: Channel has streams with NEW URLs (different stream IDs)
        new_streams = [
            {
                'id': 8917,  # NEW ID
                'name': 'MOVISTAR PLUS FHD --> NEW ERA',
                'url': 'http://acexy:8080/ace/getstream?id=NEWURL123456789',  # NEW URL
                'm3u_account': 6,
                'stream_stats': {'status': 'ok'}
            }
        ]
        
        # First call (Step 1): get current streams with OLD URLs
        # Second call (after check): get stats with NEW URLs
        mock_fetch_streams.side_effect = [old_streams, new_streams]
        
        # Mock UDI methods
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_stream_by_id = Mock(side_effect=lambda sid: 
            next((s for s in old_streams if s['id'] == sid), None))
        
        # Mock AutomatedStreamManager
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.validate_and_remove_non_matching_streams = Mock(return_value={})
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Pre-mark streams as dead with their OLD URLs (simulating previous check)
        service.dead_streams_tracker.mark_as_dead(
            'http://acexy:8080/ace/getstream?id=8c67cdb5ba81976662c3a67984a9545d9cfb0f70',
            8916,
            'MOVISTAR PLUS FHD --> NEW ERA',
            channel_id=31
        )
        service.dead_streams_tracker.mark_as_dead(
            'http://acexy:8080/ace/getstream?id=1ab443f5b4beb6d586f19e8b25b9f9646cf2ab78',
            7063,
            'MOVISTAR PLUS FHD --> ELCANO',
            channel_id=31
        )
        
        # Verify they are marked as dead
        self.assertTrue(service.dead_streams_tracker.is_dead(
            'http://acexy:8080/ace/getstream?id=8c67cdb5ba81976662c3a67984a9545d9cfb0f70'))
        self.assertTrue(service.dead_streams_tracker.is_dead(
            'http://acexy:8080/ace/getstream?id=1ab443f5b4beb6d586f19e8b25b9f9646cf2ab78'))
        
        # Get initial dead stream count for this channel
        initial_dead_count = service.dead_streams_tracker.get_dead_streams_count_for_channel(31)
        self.assertEqual(initial_dead_count, 2, "Should have 2 dead streams initially")
        
        # Mock _check_channel to avoid actual checking
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})
        
        # Call check_single_channel - this simulates the single channel check workflow
        result = service.check_single_channel(channel_id=31)
        
        # Verify the result
        self.assertTrue(result['success'], "Check should succeed")
        
        # THE KEY ASSERTION: Dead streams with OLD URLs should be cleared
        # even though the NEW stream has a different URL
        self.assertFalse(service.dead_streams_tracker.is_dead(
            'http://acexy:8080/ace/getstream?id=8c67cdb5ba81976662c3a67984a9545d9cfb0f70'),
            "Old dead stream URL should be cleared from tracker")
        self.assertFalse(service.dead_streams_tracker.is_dead(
            'http://acexy:8080/ace/getstream?id=1ab443f5b4beb6d586f19e8b25b9f9646cf2ab78'),
            "Old dead stream URL should be cleared from tracker")
        
        # Verify no dead streams remain for this channel
        final_dead_count = service.dead_streams_tracker.get_dead_streams_count_for_channel(31)
        self.assertEqual(final_dead_count, 0, "All dead streams for channel should be cleared")
        
        # Verify that playlist refresh was called
        self.assertTrue(mock_refresh.called, "Playlist refresh should be called")
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_other_channel_dead_streams_not_affected(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that clearing dead streams for one channel doesn't affect other channels."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Mock config
        mock_config = Mock()
        mock_config.get = Mock(side_effect=lambda key, default=None: default)
        mock_config_class.return_value = mock_config
        
        # Setup mocks
        mock_udi_instance = Mock()
        mock_udi.return_value = mock_udi_instance
        
        mock_channel = {
            'id': 31,
            'name': 'Channel 31',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # Mock streams for channel 31
        mock_streams_ch31 = [
            {
                'id': 101,
                'name': 'CH31 Stream',
                'url': 'http://example.com/ch31/stream1',
                'm3u_account': 1,
                'stream_stats': {'status': 'ok'}
            }
        ]
        
        mock_fetch_streams.side_effect = [mock_streams_ch31, mock_streams_ch31]
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_stream_by_id = Mock(return_value=mock_streams_ch31[0])
        
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.validate_and_remove_non_matching_streams = Mock(return_value={})
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Mark dead streams for CHANNEL 31
        service.dead_streams_tracker.mark_as_dead(
            'http://example.com/ch31/old_stream1',
            8916,
            'CH31 Old Stream',
            channel_id=31
        )
        
        # Mark dead streams for OTHER CHANNELS (should NOT be affected)
        service.dead_streams_tracker.mark_as_dead(
            'http://example.com/ch99/stream1',
            9001,
            'CH99 Stream',
            channel_id=99
        )
        service.dead_streams_tracker.mark_as_dead(
            'http://example.com/ch88/stream1',
            8801,
            'CH88 Stream',
            channel_id=88
        )
        
        # Verify all are marked as dead
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch31/old_stream1'))
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch99/stream1'))
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch88/stream1'))
        
        # Mock _check_channel
        service._check_channel = Mock(return_value={'dead_streams_count': 0, 'revived_streams_count': 0})
        
        # Call check_single_channel for channel 31
        result = service.check_single_channel(channel_id=31)
        
        # Verify the result
        self.assertTrue(result['success'])
        
        # Channel 31's dead streams should be cleared
        self.assertFalse(service.dead_streams_tracker.is_dead('http://example.com/ch31/old_stream1'),
            "Channel 31's dead stream should be cleared")
        
        # Other channels' dead streams should remain untouched
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch99/stream1'),
            "Channel 99's dead stream should remain (not affected)")
        self.assertTrue(service.dead_streams_tracker.is_dead('http://example.com/ch88/stream1'),
            "Channel 88's dead stream should remain (not affected)")
        
        # Verify counts
        self.assertEqual(service.dead_streams_tracker.get_dead_streams_count_for_channel(31), 0)
        self.assertEqual(service.dead_streams_tracker.get_dead_streams_count_for_channel(99), 1)
        self.assertEqual(service.dead_streams_tracker.get_dead_streams_count_for_channel(88), 1)


if __name__ == '__main__':
    unittest.main()

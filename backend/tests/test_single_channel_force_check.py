#!/usr/bin/env python3
"""
Test to verify that single channel check performs force checks on all streams,
bypassing the 2-hour immunity period.
"""

import unittest
from unittest.mock import Mock, patch, call
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestSingleChannelForceCheck(unittest.TestCase):
    """Test single-channel check behavior around stream-check triggering."""
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_check_does_not_mark_force_check(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """check_single_channel should not force bypass immunity; profile grace rules must apply."""
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
        
        # Mock streams with M3U accounts
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://example.com/3', 'm3u_account': 2,
             'stream_stats': {'status': 'ok'}}
        ]
        mock_fetch_streams.return_value = mock_streams
        
        # Mock UDI refresh methods
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        # Mock AutomatedStreamManager
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        
        # Mock _check_channel to avoid actual checking logic
        service._check_channel = Mock()
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify that force check was NOT marked
        assert not service.update_tracker.should_force_check(16), \
            "Channel should not be marked for force check"
        
        # Verify _check_channel was called
        service._check_channel.assert_called_once_with(
            16,
            skip_batch_changelog=True,
            run_mode='single_channel_check',
            is_single_channel_check=True,
            expected_progress_generation=0,
            force_check_override=False,
            force_check_generation=None,
        )

    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_force_check_uses_owned_override_without_consuming_queue_intent(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Explicit force_check should make the synchronous check re-analyze all streams."""
        from apps.stream.stream_checker_service import StreamCheckerService

        mock_config = Mock()
        mock_config.get = Mock(side_effect=lambda key, default=None: default)
        mock_config_class.return_value = mock_config

        mock_udi_instance = Mock()
        mock_udi.return_value = mock_udi_instance
        mock_udi_instance.get_channel_by_id.return_value = {
            'id': 16,
            'name': 'DAZN F1',
            'logo_id': None
        }

        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/2', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}},
        ]
        mock_fetch_streams.return_value = mock_streams
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)

        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})

        service = StreamCheckerService()

        # A separately queued force intent belongs to the future worker run.
        # This direct operation must neither rely on nor consume that marker.
        service.update_tracker.mark_channel_for_force_check(16)

        def fake_check(channel_id, **kwargs):
            assert kwargs['force_check_override'] is True
            assert service.update_tracker.should_force_check(channel_id)
            return {
                'dead_streams_count': 0,
                'revived_streams_count': 0,
                'analyzed_streams': [],
            }

        service._check_channel = Mock(side_effect=fake_check)

        result = service.check_single_channel(channel_id=16, force_check=True)

        self.assertTrue(result['success'])
        self.assertTrue(service.update_tracker.should_force_check(16))
        service._check_channel.assert_called_once_with(
            16,
            skip_batch_changelog=True,
            run_mode='single_channel_check',
            is_single_channel_check=True,
            expected_progress_generation=0,
            force_check_override=True,
            force_check_generation=None,
        )
        service.update_tracker.clear_force_check(16)
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_check_refreshes_playlists(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that check_single_channel refreshes M3U playlists."""
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
            'name': 'DAZN F1',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        # Mock streams with M3U accounts (two different accounts)
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/1', 'm3u_account': 5,
             'stream_stats': {'status': 'ok'}},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/2', 'm3u_account': 7,
             'stream_stats': {'status': 'ok'}}
        ]
        mock_fetch_streams.return_value = mock_streams
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        # Mock AutomatedStreamManager
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={})
        
        # Create service instance
        service = StreamCheckerService()
        service._check_channel = Mock()
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify that playlist refresh was called for each unique M3U account
        # We should see calls for accounts 5 and 7
        assert mock_refresh.called, "Playlist refresh should be called"
        
        # Check the actual calls
        refresh_calls = [call(account_id=5), call(account_id=7)]
        mock_refresh.assert_has_calls(refresh_calls, any_order=True)
        
        # Verify UDI cache was refreshed
        mock_udi_instance.refresh_streams.assert_called_once()
        mock_udi_instance.refresh_channels.assert_called_once()
    
    @patch('stream_checker_service.StreamCheckConfig')
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('automated_stream_manager.AutomatedStreamManager')
    @patch('api_utils.refresh_m3u_playlists')
    def test_single_channel_check_reassigns_streams(
        self, mock_refresh, mock_automation_class, mock_fetch_streams, mock_udi, mock_config_class
    ):
        """Test that check_single_channel re-discovers and assigns streams."""
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
            'name': 'DAZN F1',
            'logo_id': None
        }
        mock_udi_instance.get_channel_by_id.return_value = mock_channel
        
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/1', 'm3u_account': 1,
             'stream_stats': {'status': 'ok'}}
        ]
        mock_fetch_streams.return_value = mock_streams
        
        mock_udi_instance.refresh_streams = Mock()
        mock_udi_instance.refresh_channels = Mock()
        mock_udi_instance.get_streams = Mock(return_value=mock_streams)
        
        # Mock AutomatedStreamManager instance
        mock_automation_instance = Mock()
        mock_automation_class.return_value = mock_automation_instance
        mock_automation_instance.discover_and_assign_streams = Mock(return_value={'16': 5})
        
        # Create service instance
        service = StreamCheckerService()
        service._check_channel = Mock()
        
        # Call check_single_channel
        result = service.check_single_channel(channel_id=16)
        
        # Verify that discover_and_assign_streams was called with force=True and skip_check_trigger=True
        mock_automation_instance.discover_and_assign_streams.assert_called_once()
        call_kwargs = mock_automation_instance.discover_and_assign_streams.call_args[1]
        assert call_kwargs.get('force') is True, \
            "discover_and_assign_streams should be called with force=True"
        assert call_kwargs.get('skip_check_trigger') is True, \
            "discover_and_assign_streams should be called with skip_check_trigger=True"


if __name__ == '__main__':
    unittest.main()

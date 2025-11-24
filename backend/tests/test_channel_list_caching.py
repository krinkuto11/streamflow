#!/usr/bin/env python3
"""
Unit tests to verify that channel/stream list fetching is optimized.

This test verifies that when checking channels, the tool pre-fetches
stream data ONCE and passes it to update_channel_streams() to avoid
redundant API calls for each channel.

Issue: The tool was requesting the whole channel list from dispatcharr
each time it checked each stream in the global check/stream check.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestChannelListCaching(unittest.TestCase):
    """Test that stream data is pre-fetched and reused to avoid redundant API calls."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    @patch('stream_checker_service.get_streams')
    @patch('stream_checker_service.update_channel_streams')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.fetch_data_from_url')
    @patch('stream_checker_service._get_base_url')
    def test_get_streams_called_once_per_channel_check(
        self, mock_base_url, mock_fetch_data, mock_fetch_streams, 
        mock_update_channel, mock_get_streams
    ):
        """Test that get_streams is called only once per channel check, not multiple times."""
        from stream_checker_service import StreamCheckerService
        
        # Setup mocks
        mock_base_url.return_value = "http://test:8000"
        
        # Mock channel data
        mock_fetch_data.return_value = {
            'id': 1,
            'name': 'Test Channel',
            'streams': [1, 2, 3]
        }
        
        # Mock streams for the channel
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://test3'},
        ]
        mock_fetch_streams.return_value = mock_streams
        
        # Mock get_streams for pre-fetching (called once for optimization)
        mock_get_streams.return_value = mock_streams
        
        # Create service instance
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Mock stream analysis
            with patch('importlib.util.spec_from_file_location') as mock_spec:
                mock_module = MagicMock()
                mock_module._analyze_stream_task = MagicMock(return_value={
                    'channel_id': 1,
                    'channel_name': 'Test Channel',
                    'stream_id': 1,
                    'stream_name': 'Stream 1',
                    'stream_url': 'http://test1',
                    'resolution': '1920x1080',
                    'fps': 30,
                    'video_codec': 'h264',
                    'audio_codec': 'aac',
                    'bitrate_kbps': 5000,
                    'status': 'OK'
                })
                mock_module.load_config = MagicMock(return_value={})
                mock_spec.return_value.loader.exec_module = MagicMock()
                
                with patch('importlib.util.module_from_spec', return_value=mock_module):
                    with patch.object(service, '_update_stream_stats', return_value=True):
                        # Run the channel check
                        service._check_channel(1)
                        
                        # Verify get_streams was called exactly once for pre-fetching
                        self.assertEqual(mock_get_streams.call_count, 1,
                            "get_streams should be called exactly once per channel check")

    @patch('stream_checker_service.get_streams')
    @patch('stream_checker_service.update_channel_streams')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.fetch_data_from_url')
    @patch('stream_checker_service._get_base_url')
    def test_update_channel_streams_receives_precomputed_data(
        self, mock_base_url, mock_fetch_data, mock_fetch_streams, 
        mock_update_channel, mock_get_streams
    ):
        """Test that update_channel_streams receives pre-computed valid_stream_ids and stream_id_to_url."""
        from stream_checker_service import StreamCheckerService
        
        # Setup mocks
        mock_base_url.return_value = "http://test:8000"
        
        # Mock channel data
        mock_fetch_data.return_value = {
            'id': 1,
            'name': 'Test Channel',
            'streams': [1, 2]
        }
        
        # Mock streams for the channel
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
        ]
        mock_fetch_streams.return_value = mock_streams
        mock_get_streams.return_value = mock_streams
        
        # Create service instance
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Mock stream analysis
            with patch('importlib.util.spec_from_file_location') as mock_spec:
                mock_module = MagicMock()
                mock_module._analyze_stream_task = MagicMock(return_value={
                    'channel_id': 1,
                    'channel_name': 'Test Channel',
                    'stream_id': 1,
                    'stream_name': 'Stream 1',
                    'stream_url': 'http://test1',
                    'resolution': '1920x1080',
                    'fps': 30,
                    'video_codec': 'h264',
                    'audio_codec': 'aac',
                    'bitrate_kbps': 5000,
                    'status': 'OK'
                })
                mock_module.load_config = MagicMock(return_value={})
                mock_spec.return_value.loader.exec_module = MagicMock()
                
                with patch('importlib.util.module_from_spec', return_value=mock_module):
                    with patch.object(service, '_update_stream_stats', return_value=True):
                        # Run the channel check
                        service._check_channel(1)
                        
                        # Verify update_channel_streams was called with pre-computed data
                        self.assertTrue(mock_update_channel.called,
                            "update_channel_streams should be called")
                        
                        # Get the keyword arguments from the call
                        call_kwargs = mock_update_channel.call_args.kwargs
                        
                        # Verify valid_stream_ids was passed (not None)
                        self.assertIn('valid_stream_ids', call_kwargs,
                            "valid_stream_ids should be passed to update_channel_streams")
                        self.assertIsNotNone(call_kwargs['valid_stream_ids'],
                            "valid_stream_ids should not be None")
                        self.assertEqual(call_kwargs['valid_stream_ids'], {1, 2},
                            "valid_stream_ids should contain the pre-computed stream IDs")
                        
                        # Verify stream_id_to_url was passed (not None)
                        self.assertIn('stream_id_to_url', call_kwargs,
                            "stream_id_to_url should be passed to update_channel_streams")
                        self.assertIsNotNone(call_kwargs['stream_id_to_url'],
                            "stream_id_to_url should not be None")
                        expected_mapping = {1: 'http://test1', 2: 'http://test2'}
                        self.assertEqual(call_kwargs['stream_id_to_url'], expected_mapping,
                            "stream_id_to_url should contain the pre-computed URL mapping")


class TestUpdateChannelStreamsSignature(unittest.TestCase):
    """Test that update_channel_streams accepts the new stream_id_to_url parameter."""
    
    def test_update_channel_streams_accepts_stream_id_to_url(self):
        """Test that update_channel_streams signature includes stream_id_to_url parameter."""
        from api_utils import update_channel_streams
        import inspect
        
        sig = inspect.signature(update_channel_streams)
        params = list(sig.parameters.keys())
        
        self.assertIn('stream_id_to_url', params,
            "update_channel_streams should accept stream_id_to_url parameter")
        
        # Verify it's optional (has a default value, which is None)
        param = sig.parameters['stream_id_to_url']
        self.assertNotEqual(param.default, inspect.Parameter.empty,
            "stream_id_to_url should have a default value (be optional)")
    
    def test_add_streams_to_channel_accepts_stream_id_to_url(self):
        """Test that add_streams_to_channel signature includes stream_id_to_url parameter."""
        from api_utils import add_streams_to_channel
        import inspect
        
        sig = inspect.signature(add_streams_to_channel)
        params = list(sig.parameters.keys())
        
        self.assertIn('stream_id_to_url', params,
            "add_streams_to_channel should accept stream_id_to_url parameter")
        
        # Verify it's optional (has a default value, which is None)
        param = sig.parameters['stream_id_to_url']
        self.assertNotEqual(param.default, inspect.Parameter.empty,
            "stream_id_to_url should have a default value (be optional)")


class TestFilterDeadStreamsPassThrough(unittest.TestCase):
    """Test that filter_dead_streams receives and uses the stream_id_to_url mapping."""
    
    @patch('api_utils.get_dead_stream_urls')
    def test_filter_dead_streams_uses_provided_mapping(self, mock_get_dead_urls):
        """Test that filter_dead_streams uses the provided stream_id_to_url mapping."""
        from api_utils import filter_dead_streams
        
        # Mock dead URLs (empty set = no dead streams)
        mock_get_dead_urls.return_value = set()
        
        # Create a mapping
        stream_id_to_url = {1: 'http://test1', 2: 'http://test2', 3: 'http://test3'}
        
        # Call filter_dead_streams with the mapping
        filtered, count = filter_dead_streams([1, 2, 3], stream_id_to_url)
        
        # All streams should pass through (no dead streams)
        self.assertEqual(filtered, [1, 2, 3])
        self.assertEqual(count, 0)
    
    @patch('api_utils.get_dead_stream_urls')
    def test_filter_dead_streams_filters_dead_urls(self, mock_get_dead_urls):
        """Test that filter_dead_streams correctly filters out dead streams."""
        from api_utils import filter_dead_streams
        
        # Mock dead URLs - stream 2 is dead
        mock_get_dead_urls.return_value = {'http://test2'}
        
        # Create a mapping
        stream_id_to_url = {1: 'http://test1', 2: 'http://test2', 3: 'http://test3'}
        
        # Call filter_dead_streams with the mapping
        filtered, count = filter_dead_streams([1, 2, 3], stream_id_to_url)
        
        # Stream 2 should be filtered out
        self.assertEqual(filtered, [1, 3])
        self.assertEqual(count, 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)

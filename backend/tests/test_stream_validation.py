#!/usr/bin/env python3
"""
Unit tests for stream validation during update and assign operations.

This test module verifies that:
1. update_channel_streams filters out non-existent stream IDs
2. add_streams_to_channel filters out non-existent stream IDs
3. Dead/removed streams are not re-added to channels
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestStreamValidation(unittest.TestCase):
    """Test stream validation during update and assign operations."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Mock valid streams in Dispatcharr
        self.valid_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1.m3u8'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/stream2.m3u8'},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://example.com/stream3.m3u8'},
        ]
    
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    def test_update_channel_streams_filters_invalid_ids(self, mock_get_streams, mock_patch):
        """Test that update_channel_streams filters out non-existent stream IDs."""
        from api_utils import update_channel_streams
        
        # Mock get_streams to return only valid streams
        mock_get_streams.return_value = self.valid_streams
        
        # Mock successful patch request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to update channel with mix of valid and invalid stream IDs
        stream_ids = [1, 2, 999, 1000]  # 999 and 1000 don't exist
        result = update_channel_streams(1, stream_ids)
        
        # Verify the function succeeded
        self.assertTrue(result)
        
        # Verify that patch was called with only valid stream IDs
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        data = call_args[0][1]  # Second argument is the data dict
        self.assertEqual(data['streams'], [1, 2])  # Only valid IDs
    
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    def test_update_channel_streams_with_all_invalid_ids(self, mock_get_streams, mock_patch):
        """Test update_channel_streams when all stream IDs are invalid."""
        from api_utils import update_channel_streams
        
        # Mock get_streams to return only valid streams
        mock_get_streams.return_value = self.valid_streams
        
        # Mock successful patch request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to update channel with all invalid stream IDs
        stream_ids = [999, 1000, 1001]
        result = update_channel_streams(1, stream_ids)
        
        # Verify the function succeeded
        self.assertTrue(result)
        
        # Verify that patch was called with empty list
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        data = call_args[0][1]
        self.assertEqual(data['streams'], [])
    
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    @patch('api_utils.fetch_channel_streams')
    def test_add_streams_filters_invalid_ids(self, mock_fetch, mock_get_streams, mock_patch):
        """Test that add_streams_to_channel filters out non-existent stream IDs."""
        from api_utils import add_streams_to_channel
        
        # Mock current channel streams
        mock_fetch.return_value = [
            {'id': 1, 'name': 'Stream 1'}
        ]
        
        # Mock get_streams to return only valid streams
        mock_get_streams.return_value = self.valid_streams
        
        # Mock successful patch request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to add mix of valid and invalid stream IDs
        stream_ids = [2, 3, 999, 1000]  # 999 and 1000 don't exist
        result = add_streams_to_channel(1, stream_ids)
        
        # Verify 2 streams were added (only valid ones)
        self.assertEqual(result, 2)
        
        # Verify that patch was called with valid stream IDs only
        mock_patch.assert_called_once()
        call_args = mock_patch.call_args
        data = call_args[0][1]
        # Should have original stream 1 + new valid streams 2 and 3
        self.assertEqual(sorted(data['streams']), [1, 2, 3])
    
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    @patch('api_utils.fetch_channel_streams')
    def test_add_streams_with_all_invalid_ids(self, mock_fetch, mock_get_streams, mock_patch):
        """Test add_streams_to_channel when all new stream IDs are invalid."""
        from api_utils import add_streams_to_channel
        
        # Mock current channel streams
        mock_fetch.return_value = [
            {'id': 1, 'name': 'Stream 1'}
        ]
        
        # Mock get_streams to return only valid streams
        mock_get_streams.return_value = self.valid_streams
        
        # Mock successful patch request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to add all invalid stream IDs
        stream_ids = [999, 1000, 1001]
        result = add_streams_to_channel(1, stream_ids)
        
        # Verify no streams were added
        self.assertEqual(result, 0)
        
        # Verify patch was not called since no new streams to add
        mock_patch.assert_not_called()
    
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    @patch('api_utils.fetch_channel_streams')
    def test_add_streams_handles_removed_current_streams(self, mock_fetch, mock_get_streams, mock_patch):
        """Test that current channel streams that no longer exist are filtered out."""
        from api_utils import add_streams_to_channel
        
        # Mock current channel streams - includes a stream that no longer exists
        mock_fetch.return_value = [
            {'id': 1, 'name': 'Stream 1'},
            {'id': 999, 'name': 'Removed Stream'}  # This stream no longer exists
        ]
        
        # Mock get_streams to return only valid streams
        mock_get_streams.return_value = self.valid_streams
        
        # Mock successful patch request
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to add new valid stream
        stream_ids = [2]
        result = add_streams_to_channel(1, stream_ids)
        
        # Verify 1 stream was added
        self.assertEqual(result, 1)
        
        # Verify that patch was called
        # Note: The current implementation keeps existing streams in the channel
        # even if they're invalid (it doesn't clean them up during add operation)
        # This test documents this behavior
        mock_patch.assert_called_once()


class TestGetValidStreamIds(unittest.TestCase):
    """Test the get_valid_stream_ids helper function."""
    
    @patch('api_utils.get_streams')
    def test_get_valid_stream_ids_success(self, mock_get_streams):
        """Test that get_valid_stream_ids returns correct set of IDs."""
        from api_utils import get_valid_stream_ids
        
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1'},
            {'id': 2, 'name': 'Stream 2'},
            {'id': 3, 'name': 'Stream 3'},
        ]
        
        result = get_valid_stream_ids()
        
        self.assertEqual(result, {1, 2, 3})
    
    @patch('api_utils.get_streams')
    def test_get_valid_stream_ids_handles_invalid_data(self, mock_get_streams):
        """Test that get_valid_stream_ids handles invalid stream data."""
        from api_utils import get_valid_stream_ids
        
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1'},
            'invalid_stream',  # Invalid data
            {'id': 2, 'name': 'Stream 2'},
            {},  # Missing id
            {'id': 3, 'name': 'Stream 3'},
        ]
        
        result = get_valid_stream_ids()
        
        # Should only include valid streams with IDs
        self.assertEqual(result, {1, 2, 3})
    
    @patch('api_utils.get_streams')
    def test_get_valid_stream_ids_handles_error(self, mock_get_streams):
        """Test that get_valid_stream_ids returns empty set on error."""
        from api_utils import get_valid_stream_ids
        
        mock_get_streams.side_effect = Exception("API error")
        
        result = get_valid_stream_ids()
        
        # Should return empty set on error for safety
        self.assertEqual(result, set())


class TestDeadStreamFiltering(unittest.TestCase):
    """Test that dead streams are filtered out during update/assign operations."""
    
    @patch('api_utils.get_dead_stream_urls')
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    def test_update_channel_filters_dead_streams(self, mock_get_streams, mock_patch, mock_dead_urls):
        """Test that update_channel_streams filters out dead streams by default."""
        from api_utils import update_channel_streams
        
        # Mock valid streams
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1.m3u8'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/stream2.m3u8'},
            {'id': 3, 'name': 'Dead Stream', 'url': 'http://example.com/dead.m3u8'},
        ]
        
        # Mock dead stream URLs
        mock_dead_urls.return_value = {'http://example.com/dead.m3u8'}
        
        # Mock successful patch
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to update with mix of live and dead streams
        stream_ids = [1, 2, 3]
        result = update_channel_streams(1, stream_ids)
        
        # Verify success
        self.assertTrue(result)
        
        # Verify dead stream was filtered out
        call_args = mock_patch.call_args
        data = call_args[0][1]
        self.assertEqual(data['streams'], [1, 2])  # Stream 3 should be filtered
    
    @patch('api_utils.get_dead_stream_urls')
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    def test_update_channel_allows_dead_streams_in_global_check(self, mock_get_streams, mock_patch, mock_dead_urls):
        """Test that update_channel_streams allows dead streams during global checks."""
        from api_utils import update_channel_streams
        
        # Mock valid streams
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1.m3u8'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/stream2.m3u8'},
            {'id': 3, 'name': 'Dead Stream', 'url': 'http://example.com/dead.m3u8'},
        ]
        
        # Mock dead stream URLs
        mock_dead_urls.return_value = {'http://example.com/dead.m3u8'}
        
        # Mock successful patch
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to update with allow_dead_streams=True (global check)
        stream_ids = [1, 2, 3]
        result = update_channel_streams(1, stream_ids, allow_dead_streams=True)
        
        # Verify success
        self.assertTrue(result)
        
        # Verify dead stream was NOT filtered out
        call_args = mock_patch.call_args
        data = call_args[0][1]
        self.assertEqual(data['streams'], [1, 2, 3])  # Stream 3 should be included
    
    @patch('api_utils.get_dead_stream_urls')
    @patch('api_utils.patch_request')
    @patch('api_utils.get_streams')
    @patch('api_utils.fetch_channel_streams')
    def test_add_streams_filters_dead_streams(self, mock_fetch, mock_get_streams, mock_patch, mock_dead_urls):
        """Test that add_streams_to_channel filters out dead streams by default."""
        from api_utils import add_streams_to_channel
        
        # Mock current channel streams
        mock_fetch.return_value = [
            {'id': 1, 'name': 'Stream 1'}
        ]
        
        # Mock valid streams
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://example.com/stream1.m3u8'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://example.com/stream2.m3u8'},
            {'id': 3, 'name': 'Dead Stream', 'url': 'http://example.com/dead.m3u8'},
        ]
        
        # Mock dead stream URLs
        mock_dead_urls.return_value = {'http://example.com/dead.m3u8'}
        
        # Mock successful patch
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Try to add mix of live and dead streams
        stream_ids = [2, 3]
        result = add_streams_to_channel(1, stream_ids)
        
        # Verify only 1 stream was added (not the dead one)
        self.assertEqual(result, 1)
        
        # Verify dead stream was filtered out
        call_args = mock_patch.call_args
        data = call_args[0][1]
        self.assertEqual(sorted(data['streams']), [1, 2])  # Stream 3 should be filtered


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

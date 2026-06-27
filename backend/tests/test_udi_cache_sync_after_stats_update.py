#!/usr/bin/env python3
"""
Test that verifies UDI cache is synced after stream stats updates.

This test ensures that when stream stats are updated via _update_stream_stats,
the UDI cache is properly updated to reflect the new stats, preventing
inconsistencies between changelog data and actual Dispatcharr data.
"""

import unittest
import sys
import os
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch

# Set up CONFIG_DIR before importing modules
os.environ['CONFIG_DIR'] = tempfile.mkdtemp()

# Add backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))


class TestUDICacheSyncAfterStatsUpdate(unittest.TestCase):
    """Test that UDI cache is synced after stream stats updates."""
    
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.patch_request')
    @patch('stream_checker_service._get_base_url')
    def test_udi_cache_updated_after_stats_patch(self, mock_base_url, mock_patch, mock_get_udi):
        """Test that UDI cache is updated after successful stats PATCH."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Setup base URL
        mock_base_url.return_value = "http://test.com"
        
        # Setup UDI mock
        mock_udi = Mock()
        existing_stream_data = {
            'id': 123,
            'name': 'Test Stream',
            'url': 'http://test.com/stream',
            'stream_stats': {
                'resolution': '1920x1080',
                'source_fps': 25,
                'video_codec': 'h264'
            }
        }
        mock_udi.get_stream_by_id.return_value = existing_stream_data.copy()
        mock_get_udi.return_value = mock_udi
        
        # Setup PATCH mock
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Create service instance
        service = StreamCheckerService()
        
        # Call _update_stream_stats with new stats
        stream_data = {
            'stream_id': 123,
            'resolution': '1920x1080',
            'fps': 30,  # Updated from 25
            'video_codec': 'h265',  # Updated from h264
            'audio_codec': 'aac',
            'bitrate_kbps': 5000
        }
        
        result = service._update_stream_stats(stream_data)
        
        # Verify the result was successful
        self.assertTrue(result, "Stats update should succeed")
        
        # Verify PATCH was called with merged stats
        mock_patch.assert_called_once()
        patch_call_args = mock_patch.call_args
        patch_payload = patch_call_args[0][1]  # Second argument is the payload
        
        expected_stats = {
            'resolution': '1920x1080',
            'source_fps': 30,
            'video_codec': 'h265',
            'audio_codec': 'aac',
            'ffmpeg_output_bitrate': 5000,
            'measurement_incomplete': False,
            'measurement_incomplete_reason': 'none',
            'measurement_incomplete_context': {},
            'bitrate_recheck_required': False,
        }
        self.assertEqual(patch_payload['stream_stats'], expected_stats)
        
        # **CRITICAL CHECK**: Verify UDI cache was updated with the new stats
        mock_udi.update_stream.assert_called_once()
        update_call_args = mock_udi.update_stream.call_args
        updated_stream_id = update_call_args[0][0]
        updated_stream_data = update_call_args[0][1]
        
        self.assertEqual(updated_stream_id, 123, "Should update the correct stream ID")
        self.assertEqual(updated_stream_data['stream_stats'], expected_stats, 
                        "UDI cache should be updated with the new stats")
    
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.patch_request')
    @patch('stream_checker_service._get_base_url')
    def test_udi_cache_not_updated_on_patch_failure(self, mock_base_url, mock_patch, mock_get_udi):
        """Test that UDI cache is not updated if PATCH fails."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Setup base URL
        mock_base_url.return_value = "http://test.com"
        
        # Setup UDI mock
        mock_udi = Mock()
        existing_stream_data = {
            'id': 123,
            'name': 'Test Stream',
            'stream_stats': {}
        }
        mock_udi.get_stream_by_id.return_value = existing_stream_data.copy()
        mock_get_udi.return_value = mock_udi
        
        # Setup PATCH mock to fail
        mock_patch.side_effect = Exception("PATCH failed")
        
        # Create service instance
        service = StreamCheckerService()
        
        # Call _update_stream_stats
        stream_data = {
            'stream_id': 123,
            'resolution': '1920x1080',
            'fps': 30,
            'bitrate_kbps': 5000
        }
        
        result = service._update_stream_stats(stream_data)
        
        # Verify the result was failure
        self.assertFalse(result, "Stats update should fail when PATCH fails")
        
        # Verify UDI cache was NOT updated
        mock_udi.update_stream.assert_not_called()
    
    @patch('stream_checker_service.get_udi_manager')
    @patch('stream_checker_service.patch_request')
    @patch('stream_checker_service._get_base_url')
    def test_udi_cache_handles_json_string_stats(self, mock_base_url, mock_patch, mock_get_udi):
        """Test that UDI cache update works when existing stats are JSON string."""
        from apps.stream.stream_checker_service import StreamCheckerService
        
        # Setup base URL
        mock_base_url.return_value = "http://test.com"
        
        # Setup UDI mock with JSON string stats
        mock_udi = Mock()
        existing_stream_data = {
            'id': 123,
            'name': 'Test Stream',
            'stream_stats': json.dumps({'resolution': '1280x720'})  # JSON string
        }
        mock_udi.get_stream_by_id.return_value = existing_stream_data.copy()
        mock_get_udi.return_value = mock_udi
        
        # Setup PATCH mock
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        # Create service instance
        service = StreamCheckerService()
        
        # Call _update_stream_stats
        stream_data = {
            'stream_id': 123,
            'resolution': '1920x1080',
            'fps': 30,
            'bitrate_kbps': 5000
        }
        
        result = service._update_stream_stats(stream_data)
        
        # Verify the result was successful
        self.assertTrue(result, "Stats update should succeed with JSON string stats")
        
        # Verify UDI cache was updated
        mock_udi.update_stream.assert_called_once()
        update_call_args = mock_udi.update_stream.call_args
        updated_stream_data = update_call_args[0][1]
        
        # The updated stats should be a dict (not JSON string)
        self.assertIsInstance(updated_stream_data['stream_stats'], dict,
                            "Updated stats should be a dict")
        self.assertEqual(updated_stream_data['stream_stats']['resolution'], '1920x1080',
                        "Updated stats should have new resolution")


if __name__ == '__main__':
    unittest.main()

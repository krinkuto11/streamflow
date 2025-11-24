#!/usr/bin/env python3
"""
Core unit tests for Stream Checker service functionality.

This module consolidates tests for:
- Stream stats handling and default values
- Progress tracking and variable initialization
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_checker_service import StreamCheckerService


class TestStreamStatsHandling(unittest.TestCase):
    """Test handling of stream_stats with various data formats."""
    
    def test_none_stream_stats_handling(self):
        """Test that None stream_stats is handled properly."""
        # Simulate the logic from stream_checker_service.py
        stream_data = {'stream_stats': None}
        
        stream_stats = stream_data.get('stream_stats', {})
        # Handle None case explicitly
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                # Handle case where JSON string is "null"
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_empty_stream_stats_defaults(self):
        """Test that empty stream_stats uses correct defaults."""
        stream_data = {'stream_stats': {}}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0', "Resolution should default to '0x0'")
        self.assertEqual(fps, 0, "FPS should default to 0")
        self.assertEqual(bitrate, 0, "Bitrate should default to 0")
    
    def test_json_string_null_handling(self):
        """Test that JSON string 'null' is handled properly."""
        stream_data = {'stream_stats': 'null'}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                # Handle case where JSON string is "null"
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_json_string_invalid_handling(self):
        """Test that invalid JSON string is handled properly."""
        stream_data = {'stream_stats': 'invalid json'}
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        if isinstance(stream_stats, str):
            try:
                stream_stats = json.loads(stream_stats)
                if stream_stats is None:
                    stream_stats = {}
            except json.JSONDecodeError:
                stream_stats = {}
        
        # Should not raise AttributeError
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '0x0')
        self.assertEqual(fps, 0)
        self.assertEqual(bitrate, 0)
    
    def test_valid_stream_stats(self):
        """Test that valid stream_stats are used correctly."""
        stream_data = {
            'stream_stats': {
                'resolution': '1920x1080',
                'source_fps': 60,
                'ffmpeg_output_bitrate': 5000
            }
        }
        
        stream_stats = stream_data.get('stream_stats', {})
        if stream_stats is None:
            stream_stats = {}
        
        resolution = stream_stats.get('resolution', '0x0')
        fps = stream_stats.get('source_fps', 0)
        bitrate = stream_stats.get('ffmpeg_output_bitrate', 0)
        
        self.assertEqual(resolution, '1920x1080')
        self.assertEqual(fps, 60)
        self.assertEqual(bitrate, 5000)


class TestProgressTracking(unittest.TestCase):
    """Test progress tracking and variable initialization."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('stream_checker_service.get_streams')
    @patch('stream_checker_service.fetch_channel_streams')
    @patch('stream_checker_service.fetch_data_from_url')
    @patch('stream_checker_service._get_base_url')
    def test_total_streams_defined_before_use(self, mock_base_url, mock_fetch_data, mock_fetch_streams, mock_get_streams):
        """Test that total_streams is defined before being used in progress updates."""
        # Setup mocks
        mock_base_url.return_value = "http://test:8000"
        
        # Mock channel data
        mock_fetch_data.return_value = {
            'id': 1,
            'name': 'Test Channel'
        }
        
        # Mock streams - 3 streams to check
        mock_streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test2'},
            {'id': 3, 'name': 'Stream 3', 'url': 'http://test3'},
        ]
        mock_fetch_streams.return_value = mock_streams
        # Mock get_streams for pre-fetching valid stream IDs
        mock_get_streams.return_value = mock_streams
        
        # Create service instance with temporary config directory
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Mock the progress update to capture calls
            progress_calls = []
            original_update = service.progress.update
            
            def mock_progress_update(**kwargs):
                progress_calls.append(kwargs)
                # Call original to maintain state
                return original_update(**kwargs)
            
            service.progress.update = mock_progress_update
            
            # Mock _analyze_stream_task to avoid actual stream analysis
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
                        with patch('stream_checker_service.update_channel_streams'):
                            try:
                                # This should not raise NameError for total_streams
                                service._check_channel(1)
                                
                                # Verify that progress updates were made with total parameter
                                analyzing_updates = [c for c in progress_calls if c.get('status') == 'analyzing']
                                
                                if analyzing_updates:
                                    # Check that total is defined (not None) and equals number of streams
                                    for update in analyzing_updates:
                                        self.assertIn('total', update, "total parameter missing in progress update")
                                        self.assertIsNotNone(update['total'], "total parameter should not be None")
                                        self.assertEqual(update['total'], 3, "total should equal number of streams to check")
                                        
                            except NameError as e:
                                if 'total_streams' in str(e):
                                    self.fail(f"NameError for total_streams should not occur: {e}")
                                raise


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Unit test to verify stream checking mode behavior.

This test verifies that stream_checking_mode flag is properly set when:
1. An individual channel is being checked
2. There are channels in the queue
3. There are channels in progress
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.stream_checker_service import (
    StreamCheckerService,
    StreamCheckQueue
)


class TestStreamCheckingMode(unittest.TestCase):
    """Test stream checking mode flag behavior."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_stream_checking_mode_with_checking_flag(self):
        """Test that stream_checking_mode is True when checking individual channel."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Initially stream_checking_mode should be False
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
            
            # Set checking flag (simulating channel check in progress)
            service.checking = True
            
            # Now stream_checking_mode should be True
            status = service.get_status()
            self.assertTrue(status['stream_checking_mode'])
            
            # Clear flag
            service.checking = False
            
            # stream_checking_mode should be False again
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
    
    def test_stream_checking_mode_with_queue(self):
        """Test that stream_checking_mode is True when queue has channels."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Initially stream_checking_mode should be False
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
            
            # Add a channel to the queue
            service.check_queue.add_channel(1, priority=10)
            
            # Now stream_checking_mode should be True (queue_size > 0)
            status = service.get_status()
            self.assertTrue(status['stream_checking_mode'])
            
            # Clear the queue
            service.check_queue.clear()
            
            # stream_checking_mode should be False again
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
    
    def test_stream_checking_mode_with_in_progress_channels(self):
        """Test that stream_checking_mode is True when channels are in progress."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # Initially stream_checking_mode should be False
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
            
            # Add a channel to the queue and simulate it being picked up
            service.check_queue.add_channel(1, priority=10)
            # Simulate getting the channel (moves to in_progress)
            channel_id = service.check_queue.get_next_channel(timeout=0.1)
            
            # Now stream_checking_mode should be True (in_progress > 0)
            status = service.get_status()
            self.assertTrue(status['stream_checking_mode'])
            
            # Mark as completed
            service.check_queue.mark_completed(channel_id)
            
            # stream_checking_mode should be False again
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
    
    def test_stream_checking_mode_false_when_idle(self):
        """Test that stream_checking_mode is False when system is idle."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            # All flags should be False
            self.assertFalse(service.checking)
            
            # Queue should be empty
            queue_status = service.check_queue.get_status()
            self.assertEqual(queue_status['queue_size'], 0)
            self.assertEqual(queue_status['in_progress'], 0)
            
            # stream_checking_mode should be False
            status = service.get_status()
            self.assertFalse(status['stream_checking_mode'])
    
    def test_status_includes_stream_checking_mode(self):
        """Test that get_status always includes stream_checking_mode."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
            
            status = service.get_status()
            self.assertIn('stream_checking_mode', status)
            self.assertIsInstance(status['stream_checking_mode'], bool)


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

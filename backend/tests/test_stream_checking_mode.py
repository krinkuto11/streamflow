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


class InMemoryProgress:
    """Small progress double that keeps tests independent from the configured DB."""

    def __init__(self):
        self.data = None

    def update(self, channel_id, channel_name, current, total,
               current_stream='', status='checking', step='', step_detail='',
               streams_detail=None, stream_duration=None, is_single_channel_check=False,
               provider_profile_slots=None):
        self.data = {
            'channel_id': channel_id,
            'channel_name': channel_name,
            'current_stream': current,
            'total_streams': total,
            'percentage': round((current / total * 100) if total > 0 else 0, 1),
            'current_stream_name': current_stream,
            'status': status,
            'step': step,
            'step_detail': step_detail,
            'stream_duration': stream_duration,
            'is_single_channel_check': is_single_channel_check,
            'timestamp': 'test',
        }

    def clear(self):
        self.data = None

    def get(self):
        return self.data


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

    def make_service(self):
        """Create an isolated service instance for status-mode assertions."""
        with patch('apps.stream.stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            service = StreamCheckerService()
        service.progress = InMemoryProgress()
        return service
    
    def test_stream_checking_mode_with_checking_flag(self):
        """Test that stream_checking_mode is True when checking individual channel."""
        service = self.make_service()

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

    def test_single_channel_progress_keeps_stream_checking_mode_active(self):
        """Single-channel preparation progress must remain visible before stream probing starts."""
        service = self.make_service()

        service.progress.update(
            channel_id=123,
            channel_name='Single Channel',
            current=0,
            total=1,
            status='starting',
            step='Starting single channel check',
            is_single_channel_check=True,
        )

        status = service.get_status()

        self.assertFalse(service.checking)
        self.assertTrue(status['stream_checking_mode'])
        self.assertEqual(status['progress']['channel_id'], 123)
        self.assertTrue(status['progress']['is_single_channel_check'])
    
    def test_stream_checking_mode_with_queue(self):
        """Test that stream_checking_mode is True when a running worker has queued channels."""
        service = self.make_service()

        # Initially stream_checking_mode should be False
        status = service.get_status()
        self.assertFalse(status['stream_checking_mode'])

        # Add a channel to the queue while the worker is running.
        service.running = True
        service.check_queue.add_channel(1, priority=10)

        # Now stream_checking_mode should be True (queue_size > 0 and worker running)
        status = service.get_status()
        self.assertTrue(status['stream_checking_mode'])

        # Clear the queue
        service.check_queue.clear()

        # stream_checking_mode should be False again
        status = service.get_status()
        self.assertFalse(status['stream_checking_mode'])
    
    def test_stream_checking_mode_with_in_progress_channels(self):
        """Test that stream_checking_mode is True when channels are in progress."""
        service = self.make_service()

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
        service = self.make_service()

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
        service = self.make_service()

        status = service.get_status()
        self.assertIn('stream_checking_mode', status)
        self.assertIsInstance(status['stream_checking_mode'], bool)


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

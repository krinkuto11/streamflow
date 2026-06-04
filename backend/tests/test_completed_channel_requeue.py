#!/usr/bin/env python3
"""
Unit test to verify that completed channels can be re-queued when they receive new streams.

This test verifies the fix for the issue where channels that were previously checked
and marked as completed could not be re-queued when new streams were added to them.
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apps.stream.stream_checker_service import StreamCheckQueue, ChannelUpdateTracker, CONFIG_DIR


class TestCompletedChannelRequeue(unittest.TestCase):
    """Test that completed channels can be re-queued when they receive new streams."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.tracker_file = Path(self.temp_dir) / 'channel_updates.json'
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_completed_channel_can_be_requeued_with_remove_from_completed(self):
        """Test that a completed channel can be re-queued after calling remove_from_completed."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channel
        result1 = queue.add_channel(1, priority=10)
        self.assertTrue(result1, "First add should succeed")
        
        # Get channel (moves to in_progress)
        channel_id = queue.get_next_channel(timeout=0.1)
        self.assertEqual(channel_id, 1, "Should get channel 1")
        
        # Mark as completed
        queue.mark_completed(channel_id)
        
        status = queue.get_status()
        self.assertEqual(status['completed'], 1, "Channel should be in completed set")
        
        # Remove from completed set
        removed = queue.remove_from_completed(1)
        self.assertTrue(removed, "Should successfully remove from completed")
        
        # Now channel should be queueable again
        result2 = queue.add_channel(1, priority=10)
        self.assertTrue(result2, "Should be able to add after removing from completed")
        
        status = queue.get_status()
        self.assertEqual(status['queued'], 1, "Channel should be queued again")
        self.assertEqual(status['completed'], 0, "Completed set should be empty")
    
    def test_remove_from_completed_returns_false_for_non_completed_channel(self):
        """Test that remove_from_completed returns False for channels not in completed set."""
        queue = StreamCheckQueue(max_size=100)
        
        # Try to remove a channel that was never added
        removed = queue.remove_from_completed(999)
        self.assertFalse(removed, "Should return False for non-existent channel")
    
    def test_integration_channels_with_new_streams_can_be_checked_again(self):
        """Integration test: Simulate the full flow of checking a channel, then re-checking after new streams."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            queue = StreamCheckQueue(max_size=100)
            tracker = ChannelUpdateTracker(self.tracker_file)
            
            # Step 1: Mark channel as updated (simulating M3U refresh)
            tracker.mark_channel_updated(channel_id=1, stream_count=5)
            
            # Step 2: Get channels needing check and queue them
            channels_to_queue = tracker.get_and_clear_channels_needing_check(max_channels=50)
            self.assertEqual(len(channels_to_queue), 1)
            self.assertIn(1, channels_to_queue)
            
            added = queue.add_channels(channels_to_queue, priority=10)
            self.assertEqual(added, 1, "Channel should be added to queue")
            
            # Step 3: Process the channel
            channel_id = queue.get_next_channel(timeout=0.1)
            self.assertEqual(channel_id, 1)
            
            # Step 4: Mark channel as completed (simulating successful check)
            queue.mark_completed(channel_id)
            tracker.mark_channel_checked(channel_id=1, stream_count=5, checked_stream_ids=[101, 102, 103, 104, 105])
            
            status = queue.get_status()
            self.assertEqual(status['completed'], 1, "Channel should be marked as completed")
            
            # Step 5: New streams are discovered and added to the channel
            tracker.mark_channel_updated(channel_id=1, stream_count=7)  # 2 new streams added
            
            # Step 6: Try to queue updated channels (THIS IS THE KEY SCENARIO)
            channels_to_queue = tracker.get_and_clear_channels_needing_check(max_channels=50)
            self.assertEqual(len(channels_to_queue), 1)
            self.assertIn(1, channels_to_queue)
            
            # Step 7: Remove from completed set before re-queueing (THE FIX)
            for ch_id in channels_to_queue:
                queue.remove_from_completed(ch_id)
            
            # Step 8: Now the channel should be successfully re-queued
            added = queue.add_channels(channels_to_queue, priority=10)
            self.assertEqual(added, 1, "Channel with new streams should be re-queued successfully")
            
            status = queue.get_status()
            self.assertEqual(status['queued'], 1, "Channel should be in queue again")
            self.assertEqual(status['completed'], 0, "Channel should no longer be in completed set")

    def test_update_tracker_does_not_initialize_udi_while_draining_queue(self):
        """Draining updated channels should not start UDI or require profile storage."""
        class UninitializedUdi:
            def is_initialized(self):
                return False

            def get_channels(self):
                raise AssertionError("get_channels should not be called before UDI is initialized")

        class UnavailableAutomationConfig:
            def get_effective_configuration(self, channel_id, group_id=None):
                raise RuntimeError("profile storage is not ready")

            def get_all_profiles(self, *args, **kwargs):
                raise RuntimeError("profile storage is not ready")

        tracker = ChannelUpdateTracker(self.tracker_file)
        tracker.updates = {
            'channels': {
                '42': {
                    'last_update': '2026-06-03T00:00:00',
                    'needs_check': True,
                    'stream_count': 3,
                    'checked_stream_ids': [],
                },
            },
            'last_global_check': None,
        }

        with patch('apps.stream.stream_checker_components.get_udi_manager', return_value=UninitializedUdi()), \
             patch('apps.automation.automation_config_manager.get_automation_config_manager', return_value=UnavailableAutomationConfig()):
            channels_to_queue = tracker.get_and_clear_channels_needing_check(max_channels=50)

        self.assertEqual(channels_to_queue, [42])
    
    def test_integration_without_fix_channels_cannot_be_requeued(self):
        """Integration test showing the problem WITHOUT the fix (remove_from_completed not called)."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            queue = StreamCheckQueue(max_size=100)
            tracker = ChannelUpdateTracker(self.tracker_file)
            
            # Complete the same flow as above, but skip the remove_from_completed step
            tracker.mark_channel_updated(channel_id=1, stream_count=5)
            channels_to_queue = tracker.get_and_clear_channels_needing_check(max_channels=50)
            queue.add_channels(channels_to_queue, priority=10)
            
            channel_id = queue.get_next_channel(timeout=0.1)
            queue.mark_completed(channel_id)
            tracker.mark_channel_checked(channel_id=1, stream_count=5, checked_stream_ids=[101, 102, 103, 104, 105])
            
            # New streams added
            tracker.mark_channel_updated(channel_id=1, stream_count=7)
            channels_to_queue = tracker.get_and_clear_channels_needing_check(max_channels=50)
            
            # Try to add WITHOUT calling remove_from_completed (this is the bug)
            added = queue.add_channels(channels_to_queue, priority=10)
            self.assertEqual(added, 0, "Without the fix, 0 channels should be added (bug behavior)")
            
            status = queue.get_status()
            self.assertEqual(status['completed'], 1, "Channel still in completed set")
            self.assertEqual(status['queued'], 0, "Channel not re-queued (bug behavior)")


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""
Unit test to verify that channels cannot be queued multiple times.

This test verifies that the fix for the channel stacking issue works correctly
by ensuring that channels already in the queue cannot be added again.
"""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apps.stream.stream_checker_service import StreamCheckQueue


class TestQueueNoDuplicates(unittest.TestCase):
    """Test that channels cannot be queued multiple times."""
    
    def test_add_channel_prevents_duplicates(self):
        """Test that adding the same channel twice only adds it once."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channel first time - should succeed
        result1 = queue.add_channel(1, priority=10)
        self.assertTrue(result1, "First add should succeed")
        
        # Try to add same channel again - should fail
        result2 = queue.add_channel(1, priority=10)
        self.assertFalse(result2, "Second add should fail (already queued)")
        
        # Queue should only have 1 item
        status = queue.get_status()
        self.assertEqual(status['queue_size'], 1, "Queue should have exactly 1 item")
        self.assertEqual(status['queued'], 1, "Queued set should have 1 channel")
    
    def test_add_channels_prevents_duplicates(self):
        """Test that adding multiple channels prevents duplicates."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channels [1, 2, 3]
        added1 = queue.add_channels([1, 2, 3], priority=10)
        self.assertEqual(added1, 3, "Should add all 3 channels")
        
        # Try to add [2, 3, 4] - only 4 should be added
        added2 = queue.add_channels([2, 3, 4], priority=10)
        self.assertEqual(added2, 1, "Should only add channel 4 (2 and 3 already queued)")
        
        # Queue should have 4 items total
        status = queue.get_status()
        self.assertEqual(status['queue_size'], 4, "Queue should have exactly 4 items")
        self.assertEqual(status['queued'], 4, "Queued set should have 4 channels")
    
    def test_manual_check_all_prevents_duplicates(self):
        """Test that clicking 'check all' multiple times doesn't create duplicates."""
        queue = StreamCheckQueue(max_size=100)
        
        # Simulate first "check all channels" click
        channel_ids = [1, 2, 3, 4, 5]
        added1 = queue.add_channels(channel_ids, priority=10)
        self.assertEqual(added1, 5, "First check all should add all 5 channels")
        
        # Simulate second "check all channels" click (before processing starts)
        added2 = queue.add_channels(channel_ids, priority=10)
        self.assertEqual(added2, 0, "Second check all should add 0 channels (all already queued)")
        
        # Queue should still have only 5 items
        status = queue.get_status()
        self.assertEqual(status['queue_size'], 5, "Queue should still have exactly 5 items")
        self.assertEqual(status['queued'], 5, "Queued set should have 5 channels")
        self.assertEqual(status['total_queued'], 5, "Total queued stat should be 5")

    def test_add_channels_does_not_initialize_udi_for_stream_counts(self):
        """Queue ETA counts should not trigger UDI auto-init or network fetches."""
        queue = StreamCheckQueue(max_size=100)

        class UninitializedUdi:
            def is_initialized(self):
                return False

            def get_channel_by_id(self, channel_id, fetch_if_missing=True):
                raise AssertionError("queue stream-count lookup must stay cache-only")

        with patch('apps.udi.get_udi_manager', return_value=UninitializedUdi()):
            added = queue.add_channels([1, 2, 3], priority=10)

        self.assertEqual(added, 3)
        status = queue.get_status()
        self.assertEqual(status['queued_streams_count'], 3)

    def test_add_channels_uses_cache_only_udi_lookup_for_stream_counts(self):
        """When UDI is initialized, stream counts use cached data only."""
        queue = StreamCheckQueue(max_size=100)

        class InitializedUdi:
            def __init__(self):
                self.fetch_flags = []

            def is_initialized(self):
                return True

            def get_channel_by_id(self, channel_id, fetch_if_missing=True):
                self.fetch_flags.append(fetch_if_missing)
                streams_by_channel = {
                    1: [10, 11, 12],
                    2: [20, 21],
                }
                return {'id': channel_id, 'streams': streams_by_channel.get(channel_id, [])}

        udi = InitializedUdi()
        with patch('apps.udi.get_udi_manager', return_value=udi):
            added = queue.add_channels([1, 2], priority=10)

        self.assertEqual(added, 2)
        self.assertEqual(udi.fetch_flags, [False, False])
        status = queue.get_status()
        self.assertEqual(status['queued_streams_count'], 5)
    
    def test_channel_can_be_requeued_after_completion(self):
        """Test that a channel can be queued again after it's completed."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channel
        result1 = queue.add_channel(1, priority=10)
        self.assertTrue(result1, "First add should succeed")
        
        # Get channel (moves to in_progress)
        channel_id = queue.get_next_channel(timeout=0.1)
        self.assertEqual(channel_id, 1, "Should get channel 1")
        
        status = queue.get_status()
        self.assertEqual(status['queued'], 0, "No channels should be in queued set")
        self.assertEqual(status['in_progress'], 1, "Channel should be in progress")
        
        # Mark as completed
        queue.mark_completed(channel_id)
        
        # Try to add same channel again - should fail (already completed)
        result2 = queue.add_channel(1, priority=10)
        self.assertFalse(result2, "Cannot re-add completed channel")
    
    def test_channel_can_be_requeued_after_clear(self):
        """Test that channels can be queued again after clearing the queue."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channel
        result1 = queue.add_channel(1, priority=10)
        self.assertTrue(result1, "First add should succeed")
        
        # Clear queue
        queue.clear()
        
        # Channel should be queueable again
        result2 = queue.add_channel(1, priority=10)
        self.assertTrue(result2, "Should be able to add after clear")
        
        status = queue.get_status()
        self.assertEqual(status['queued'], 1, "Queued set should have 1 channel")
    
    def test_get_next_channel_removes_from_queued(self):
        """Test that get_next_channel removes channel from queued set."""
        queue = StreamCheckQueue(max_size=100)
        
        # Add channels
        queue.add_channels([1, 2, 3], priority=10)
        
        status = queue.get_status()
        self.assertEqual(status['queued'], 3, "Should have 3 channels in queued set")
        
        # Get first channel
        channel_id = queue.get_next_channel(timeout=0.1)
        self.assertIsNotNone(channel_id, "Should get a channel")
        
        # Check that it was removed from queued set
        status = queue.get_status()
        self.assertEqual(status['queued'], 2, "Should have 2 channels left in queued set")
        self.assertEqual(status['in_progress'], 1, "Should have 1 channel in progress")


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

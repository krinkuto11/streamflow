#!/usr/bin/env python3
"""
Simple verification script to test stream checking mode API behavior.
This can be run manually to verify the implementation works.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stream_checker_service import StreamCheckerService
import tempfile
from pathlib import Path
from unittest.mock import patch

def test_stream_checking_mode():
    """Verify stream_checking_mode is correctly computed in various scenarios."""
    
    temp_dir = tempfile.mkdtemp()
    
    try:
        with patch('stream_checker_service.CONFIG_DIR', Path(temp_dir)):
            service = StreamCheckerService()
            
            print("✓ StreamCheckerService initialized")
            
            # Test 1: Idle state
            status = service.get_status()
            assert 'stream_checking_mode' in status, "stream_checking_mode missing from status"
            assert status['stream_checking_mode'] == False, "Expected False when idle"
            print("✓ Test 1: stream_checking_mode is False when idle")
            
            # Test 2: Global action in progress
            service.global_action_in_progress = True
            status = service.get_status()
            assert status['stream_checking_mode'] == True, "Expected True during global action"
            service.global_action_in_progress = False
            print("✓ Test 2: stream_checking_mode is True during global action")
            
            # Test 3: Individual check in progress
            service.checking = True
            status = service.get_status()
            assert status['stream_checking_mode'] == True, "Expected True during individual check"
            service.checking = False
            print("✓ Test 3: stream_checking_mode is True during individual check")
            
            # Test 4: Queue has channels
            service.check_queue.add_channel(1, priority=10)
            status = service.get_status()
            assert status['stream_checking_mode'] == True, "Expected True when queue has channels"
            service.check_queue.clear()
            print("✓ Test 4: stream_checking_mode is True when queue has channels")
            
            # Test 5: Channel in progress
            service.check_queue.add_channel(1, priority=10)
            channel_id = service.check_queue.get_next_channel(timeout=0.1)
            status = service.get_status()
            assert status['stream_checking_mode'] == True, "Expected True when channel in progress"
            service.check_queue.mark_completed(channel_id)
            print("✓ Test 5: stream_checking_mode is True when channel in progress")
            
            # Test 6: Back to idle
            status = service.get_status()
            assert status['stream_checking_mode'] == False, "Expected False when back to idle"
            print("✓ Test 6: stream_checking_mode is False when back to idle")
            
            print("\n✅ All verification tests passed!")
            print("\nStream checking mode correctly reflects:")
            print("  - Global actions in progress")
            print("  - Individual channel checks")
            print("  - Queued channels")
            print("  - Channels being processed")
            
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == '__main__':
    test_stream_checking_mode()

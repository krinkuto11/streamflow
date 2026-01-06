#!/usr/bin/env python3
"""
Test AceStream Dead Stream Retry Fix

Verifies that dead streams don't restart immediately and only retry after
the configured interval, and that health status is properly managed.
"""

import sys
import os
import time
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from acestream_http_monitor import HTTPStreamKeepAlive
from acestream_monitor_service import AceStreamMonitor


class TestDeadStreamRetryFix(unittest.TestCase):
    """Test cases for dead stream retry logic."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.http_keepalive = HTTPStreamKeepAlive()
        
        # Mock UDI manager
        self.mock_udi = Mock()
        self.mock_udi.get_channels.return_value = []
        self.mock_udi.get_stream_by_id.return_value = {
            'id': 123,
            'name': 'Test Stream',
            'url': 'http://localhost:6878/ace/getstream?id=test123'
        }
        
        # Mock database
        with patch('acestream_monitor_service.AceStreamDatabase'):
            # Create monitor with short retry interval for testing
            self.monitor = AceStreamMonitor(
                udi_manager=self.mock_udi,
                config={
                    'monitoring_method': 'http',
                    'dead_stream_retry_interval': 2,  # 2 seconds for testing
                    'http_keepalive_interval': 1
                }
            )
    
    def tearDown(self):
        """Clean up after tests."""
        self.http_keepalive.stop_all()
        if hasattr(self, 'monitor'):
            self.monitor.shutdown()
    
    def test_dead_stream_not_restarted_immediately(self):
        """Test that a stream marked as dead is not immediately restarted."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Mark stream as dead in the MONITOR's keepalive health tracking
        self.monitor.http_keepalive.stream_health[stream_id] = {
            'is_alive': False,
            'failures': 3,
            'last_failure': datetime.now(),
            'last_success': None
        }
        
        # Try to ensure keepalive - should not start because stream is dead
        with patch.object(self.monitor.http_keepalive, 'start_keepalive') as mock_start:
            self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
            
            # Verify start_keepalive was NOT called
            mock_start.assert_not_called()
        
        # Verify stream was marked as dead in monitor's tracking
        self.assertIn(stream_id, self.monitor.dead_stream_retry_times)
    
    def test_dead_stream_retries_after_interval(self):
        """Test that a dead stream retries after the configured interval."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Mark stream as dead and set retry time in the past
        past_time = datetime.now() - timedelta(seconds=5)
        self.monitor.dead_stream_retry_times[stream_id] = past_time
        
        # Try to ensure keepalive - should start because interval has passed
        with patch.object(self.monitor.http_keepalive, 'start_keepalive') as mock_start:
            with patch.object(self.monitor.http_keepalive, 'is_stream_alive', return_value=False):
                self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
                
                # Verify start_keepalive WAS called
                mock_start.assert_called_once()
        
        # Verify retry time was cleared
        self.assertNotIn(stream_id, self.monitor.dead_stream_retry_times)
    
    def test_health_status_cleared_on_retry(self):
        """Test that health status is cleared when retrying a dead stream."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Set up dead stream with health data
        past_time = datetime.now() - timedelta(seconds=5)
        self.monitor.dead_stream_retry_times[stream_id] = past_time
        self.monitor.http_keepalive.stream_health[stream_id] = {
            'is_alive': False,
            'failures': 3,
            'last_failure': datetime.now(),
            'last_success': None
        }
        
        # Try to ensure keepalive
        with patch.object(self.monitor.http_keepalive, 'start_keepalive'):
            with patch.object(self.monitor.http_keepalive, 'is_stream_alive', return_value=False):
                self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
        
        # Verify health status was cleared
        self.assertNotIn(stream_id, self.monitor.http_keepalive.stream_health)
    
    def test_dead_stream_does_not_retry_before_interval(self):
        """Test that a dead stream does not retry before the interval expires."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Mark stream as dead with recent retry time
        self.monitor.dead_stream_retry_times[stream_id] = datetime.now()
        
        # Try to ensure keepalive - should not start because interval hasn't passed
        with patch.object(self.http_keepalive, 'start_keepalive') as mock_start:
            self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
            
            # Verify start_keepalive was NOT called
            mock_start.assert_not_called()
    
    def test_healthy_stream_keeps_running(self):
        """Test that a healthy running stream is not stopped."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Mock stream as already running and healthy
        with patch.object(self.monitor.http_keepalive, 'is_stream_alive', return_value=True):
            with patch.object(self.monitor.http_keepalive, 'get_stream_health', return_value={
                'is_alive': True,
                'failures': 0,
                'last_success': datetime.now()
            }):
                with patch.object(self.monitor.http_keepalive, 'start_keepalive') as mock_start:
                    with patch.object(self.monitor.http_keepalive, 'stop_keepalive') as mock_stop:
                        self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
                        
                        # Verify neither start nor stop was called
                        mock_start.assert_not_called()
                        mock_stop.assert_not_called()
    
    def test_stream_with_failures_marked_dead(self):
        """Test that a stream with 3+ failures is marked as dead."""
        stream_id = 123
        stream_url = 'http://localhost:6878/ace/getstream?id=test123'
        
        # Mock stream as running but with 3 failures
        with patch.object(self.monitor.http_keepalive, 'is_stream_alive', return_value=True):
            with patch.object(self.monitor.http_keepalive, 'get_stream_health', return_value={
                'is_alive': True,
                'failures': 3,
                'last_failure': datetime.now()
            }):
                with patch.object(self.monitor.http_keepalive, 'stop_keepalive') as mock_stop:
                    self.monitor._ensure_http_keepalive(stream_id, stream_url, channel_id=1)
                    
                    # Verify stop was called
                    mock_stop.assert_called_once_with(stream_id)
        
        # Verify stream was marked for retry
        self.assertIn(stream_id, self.monitor.dead_stream_retry_times)


def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestDeadStreamRetryFix)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)

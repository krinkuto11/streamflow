#!/usr/bin/env python3
"""
Test HTTP-based AceStream Monitoring

Verifies that the HTTP range request approach works as a lightweight
alternative to ffmpeg for keeping streams alive in the orchestrator.
"""

import sys
import os
import time
import unittest
from unittest.mock import Mock, patch, MagicMock

# Add backend to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from acestream_http_monitor import HTTPStreamKeepAlive


class TestHTTPStreamKeepAlive(unittest.TestCase):
    """Test cases for HTTP-based stream keep-alive."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.keepalive = HTTPStreamKeepAlive()
    
    def tearDown(self):
        """Clean up after tests."""
        self.keepalive.stop_all()
    
    def test_initialization(self):
        """Test that HTTPStreamKeepAlive initializes correctly."""
        self.assertEqual(len(self.keepalive.active_streams), 0)
        self.assertEqual(len(self.keepalive.stream_health), 0)
    
    @patch('acestream_http_monitor.requests.Session')
    def test_start_keepalive(self, mock_session_class):
        """Test starting HTTP keep-alive for a stream."""
        # Mock successful HTTP response
        mock_response = Mock()
        mock_response.status_code = 206  # Partial Content
        mock_response.content = b'x' * 1024  # 1KB of data
        
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session
        
        # Start keep-alive
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=1,  # Short interval for testing
            chunk_size=1024
        )
        
        # Give it a moment to start
        time.sleep(0.5)
        
        # Verify stream is tracked
        self.assertIn(stream_id, self.keepalive.active_streams)
        self.assertIn(stream_id, self.keepalive.stream_health)
        
        # Verify thread is running
        thread = self.keepalive.active_streams[stream_id]['thread']
        self.assertTrue(thread.is_alive())
    
    @patch('acestream_http_monitor.requests.Session')
    def test_stop_keepalive(self, mock_session_class):
        """Test stopping HTTP keep-alive for a stream."""
        # Mock session
        mock_session = Mock()
        mock_session_class.return_value = mock_session
        
        # Start keep-alive
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=10
        )
        
        time.sleep(0.2)
        
        # Stop keep-alive
        self.keepalive.stop_keepalive(stream_id)
        
        # Verify stream is no longer tracked
        self.assertNotIn(stream_id, self.keepalive.active_streams)
    
    @patch('acestream_http_monitor.requests.Session')
    def test_health_tracking_success(self, mock_session_class):
        """Test that successful requests update health correctly."""
        # Mock successful responses
        mock_response = Mock()
        mock_response.status_code = 206
        mock_response.content = b'x' * 1024
        
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session
        
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=0.5,
            chunk_size=1024
        )
        
        # Wait for a few requests
        time.sleep(2)
        
        # Check health
        health = self.keepalive.get_stream_health(stream_id)
        self.assertIsNotNone(health)
        self.assertTrue(health.get('is_alive', False))
        self.assertEqual(health.get('failures', -1), 0)
        
        # Check stats
        stats = self.keepalive.get_stream_stats(stream_id)
        self.assertIsNotNone(stats)
        self.assertGreater(stats['stats']['requests_sent'], 0)
        self.assertGreater(stats['stats']['bytes_received'], 0)
    
    @patch('acestream_http_monitor.requests.Session')
    def test_health_tracking_failure(self, mock_session_class):
        """Test that failed requests update health correctly."""
        # Mock failed responses
        mock_session = Mock()
        mock_session.get.side_effect = Exception("Connection error")
        mock_session_class.return_value = mock_session
        
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=0.2,
            chunk_size=1024
        )
        
        # Wait for failures to accumulate
        time.sleep(1)
        
        # Check health
        health = self.keepalive.get_stream_health(stream_id)
        self.assertIsNotNone(health)
        # After 3 consecutive failures, stream should be marked as not alive
        self.assertFalse(health.get('is_alive', True))
        self.assertGreater(health.get('failures', 0), 0)
    
    @patch('acestream_http_monitor.requests.Session')
    def test_eof_detection(self, mock_session_class):
        """Test that EOF (empty response) is detected."""
        # Mock EOF response (empty content)
        mock_response = Mock()
        mock_response.status_code = 206
        mock_response.content = b''  # Empty = EOF
        
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session
        
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=0.2,
            chunk_size=1024
        )
        
        # Wait for EOF detection
        time.sleep(1)
        
        # Check that failures were tracked
        health = self.keepalive.get_stream_health(stream_id)
        self.assertIsNotNone(health)
        self.assertGreater(health.get('failures', 0), 0)
        
        # Check that last error is EOF
        stats = self.keepalive.get_stream_stats(stream_id)
        self.assertIsNotNone(stats)
        last_error = stats['stats'].get('last_error')
        if last_error:
            self.assertEqual(last_error.get('type'), 'eof')
    
    @patch('acestream_http_monitor.requests.Session')
    def test_http_error_detection(self, mock_session_class):
        """Test that HTTP errors are detected."""
        # Mock HTTP error response
        mock_response = Mock()
        mock_response.status_code = 404  # Not Found
        
        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session
        
        stream_id = 123
        stream_url = "http://localhost:6878/ace/getstream?id=test123"
        
        self.keepalive.start_keepalive(
            stream_id=stream_id,
            stream_url=stream_url,
            interval=0.2,
            chunk_size=1024
        )
        
        # Wait for error detection
        time.sleep(1)
        
        # Check that failures were tracked
        health = self.keepalive.get_stream_health(stream_id)
        self.assertIsNotNone(health)
        self.assertGreater(health.get('failures', 0), 0)
        
        # Check that last error is HTTP error
        stats = self.keepalive.get_stream_stats(stream_id)
        self.assertIsNotNone(stats)
        last_error = stats['stats'].get('last_error')
        if last_error:
            self.assertEqual(last_error.get('type'), 'http_error')
    
    def test_is_stream_alive(self):
        """Test is_stream_alive method."""
        # Non-existent stream
        self.assertFalse(self.keepalive.is_stream_alive(999))
        
        # Add a healthy stream
        stream_id = 123
        self.keepalive.stream_health[stream_id] = {
            'failures': 0,
            'last_success': None,
            'last_failure': None,
            'is_alive': True
        }
        
        self.assertTrue(self.keepalive.is_stream_alive(stream_id))
        
        # Mark as not alive
        self.keepalive.stream_health[stream_id]['is_alive'] = False
        self.assertFalse(self.keepalive.is_stream_alive(stream_id))
    
    def test_stop_all(self):
        """Test stopping all keep-alive threads."""
        # Start multiple streams (mocked)
        with patch('acestream_http_monitor.requests.Session'):
            for i in range(3):
                self.keepalive.start_keepalive(
                    stream_id=i,
                    stream_url=f"http://localhost:6878/ace/getstream?id=test{i}",
                    interval=10
                )
            
            time.sleep(0.2)
            
            # Verify all are running
            self.assertEqual(len(self.keepalive.active_streams), 3)
            
            # Stop all
            self.keepalive.stop_all()
            
            # Verify all are stopped
            self.assertEqual(len(self.keepalive.active_streams), 0)


def run_tests():
    """Run all tests."""
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestHTTPStreamKeepAlive)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)

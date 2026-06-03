#!/usr/bin/env python3
"""
Unit tests for HTTP log filtering functionality.

This module tests:
- HTTP log filter correctly filters out HTTP-related messages
"""

import unittest
import logging
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.logging_config import HTTPLogFilter


class TestHTTPLogFilter(unittest.TestCase):
    """Test HTTP log filtering functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.filter = HTTPLogFilter()
    
    def test_filter_http_request_log(self):
        """Test that HTTP request logs are filtered out."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='HTTP request GET /api/test',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))
    
    def test_filter_http_response_log(self):
        """Test that HTTP response logs are filtered out."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='HTTP response status code 200',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))
    
    def test_filter_get_request_log(self):
        """Test that GET request logs are filtered out."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='GET /api/channels',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))
    
    def test_filter_post_request_log(self):
        """Test that POST request logs are filtered out."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='POST /api/refresh-playlist',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))
    
    def test_filter_werkzeug_log(self):
        """Test that Werkzeug logs are filtered out."""
        record = logging.LogRecord(
            name='werkzeug',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='127.0.0.1 - - [07/Oct/2024 12:00:00] "GET /api/status HTTP/1.1" 200 -',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))
    
    def test_allow_non_http_log(self):
        """Test that non-HTTP logs pass through."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='Starting automation system',
            args=(),
            exc_info=None
        )
        self.assertTrue(self.filter.filter(record))
    
    def test_allow_config_log(self):
        """Test that configuration logs pass through."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='Configuration updated successfully',
            args=(),
            exc_info=None
        )
        self.assertTrue(self.filter.filter(record))
    
    def test_allow_m3u_refresh_log(self):
        """Test that M3U refresh logs pass through."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='M3U refresh request accepted by Dispatcharr',
            args=(),
            exc_info=None
        )
        self.assertTrue(self.filter.filter(record))
    
    def test_case_insensitive_filtering(self):
        """Test that filtering is case insensitive."""
        record = logging.LogRecord(
            name='test',
            level=logging.INFO,
            pathname='',
            lineno=0,
            msg='HTTP REQUEST received',
            args=(),
            exc_info=None
        )
        self.assertFalse(self.filter.filter(record))


if __name__ == '__main__':
    unittest.main()

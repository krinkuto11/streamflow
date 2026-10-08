#!/usr/bin/env python3
"""
Unit test to verify HTTP requests have timeout parameters to prevent hanging.

This test ensures that all HTTP requests in the codebase have proper timeout
parameters to prevent indefinite hangs when APIs don't respond.
"""

import unittest
import os
import sys
from unittest.mock import Mock, patch
from pathlib import Path

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestHTTPTimeout(unittest.TestCase):
    """Test that HTTP requests have timeout parameters."""
    
    @patch('apps.core.http_transport.get')
    @patch('udi.fetcher.os.getenv')
    def test_udi_fetcher_fetch_url_has_timeout(self, mock_getenv, mock_get):
        """Test that UDI fetcher _fetch_url includes timeout parameter."""
        from apps.udi.fetcher import UDIFetcher
        
        # Mock environment variables
        mock_getenv.side_effect = lambda key: {
            'DISPATCHARR_BASE_URL': 'http://test.com',
            'DISPATCHARR_TOKEN': 'test_token'
        }.get(key)
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'id': 1, 'name': 'test'}
        mock_get.return_value = mock_response
        
        fetcher = UDIFetcher()
        result = fetcher._fetch_url('http://test.com/api/test/')
        
        # Verify timeout was passed to requests.get
        self.assertTrue(mock_get.called)
        call_kwargs = mock_get.call_args[1]
        self.assertIn('timeout', call_kwargs, "requests.get should have timeout parameter")
        self.assertIsNotNone(call_kwargs['timeout'])
        self.assertGreater(call_kwargs['timeout'], 0, "Timeout should be positive")
    
    @patch('apps.core.http_transport.get')
    @patch('api_utils.os.getenv')
    def test_api_utils_fetch_data_has_timeout(self, mock_getenv, mock_get):
        """Test that api_utils fetch_data_from_url includes timeout parameter."""
        from apps.core.api_utils import fetch_data_from_url
        
        # Mock environment variables
        mock_getenv.side_effect = lambda key: {
            'DISPATCHARR_BASE_URL': 'http://test.com',
            'DISPATCHARR_TOKEN': 'test_token'
        }.get(key)
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {'test': 'data'}
        mock_get.return_value = mock_response
        
        result = fetch_data_from_url('http://test.com/api/test/')
        
        # Verify timeout was passed to requests.get
        self.assertTrue(mock_get.called)
        call_kwargs = mock_get.call_args[1]
        self.assertIn('timeout', call_kwargs, "requests.get should have timeout parameter")
        self.assertIsNotNone(call_kwargs['timeout'])
        self.assertGreater(call_kwargs['timeout'], 0, "Timeout should be positive")
    
    @patch('apps.core.http_transport.patch')
    @patch('api_utils.os.getenv')
    def test_api_utils_patch_has_timeout(self, mock_getenv, mock_patch):
        """Test that api_utils patch_request includes timeout parameter."""
        from apps.core.api_utils import patch_request
        
        # Mock environment variables
        mock_getenv.side_effect = lambda key: {
            'DISPATCHARR_BASE_URL': 'http://test.com',
            'DISPATCHARR_TOKEN': 'test_token'
        }.get(key)
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_patch.return_value = mock_response
        
        result = patch_request('http://test.com/api/test/', {'data': 'test'})
        
        # Verify timeout was passed to requests.patch
        self.assertTrue(mock_patch.called)
        call_kwargs = mock_patch.call_args[1]
        self.assertIn('timeout', call_kwargs, "requests.patch should have timeout parameter")
        self.assertIsNotNone(call_kwargs['timeout'])
        self.assertGreater(call_kwargs['timeout'], 0, "Timeout should be positive")
    
    @patch('apps.core.http_transport.post')
    @patch('api_utils.os.getenv')
    def test_api_utils_post_has_timeout(self, mock_getenv, mock_post):
        """Test that api_utils post_request includes timeout parameter."""
        from apps.core.api_utils import post_request
        
        # Mock environment variables
        mock_getenv.side_effect = lambda key: {
            'DISPATCHARR_BASE_URL': 'http://test.com',
            'DISPATCHARR_TOKEN': 'test_token'
        }.get(key)
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response
        
        result = post_request('http://test.com/api/test/', {'data': 'test'})
        
        # Verify timeout was passed to requests.post
        self.assertTrue(mock_post.called)
        call_kwargs = mock_post.call_args[1]
        self.assertIn('timeout', call_kwargs, "requests.post should have timeout parameter")
        self.assertIsNotNone(call_kwargs['timeout'])
        self.assertGreater(call_kwargs['timeout'], 0, "Timeout should be positive")


if __name__ == '__main__':
    unittest.main()

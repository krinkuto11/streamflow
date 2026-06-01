#!/usr/bin/env python3
"""
Unit test to verify token validation and caching improvements.

This test verifies that the token validation mechanism works correctly
and reduces unnecessary login attempts.
"""

import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
import sys

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TestTokenValidation(unittest.TestCase):
    """Test token validation and caching functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ.pop('DISPATCHARR_API_KEY', None)
        try:
            import apps.config.dispatcharr_config as dispatcharr_config_module
            dispatcharr_config_module._dispatcharr_config = None
        except Exception:
            pass
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_validate_token_with_valid_token(self, mock_base_url, mock_get):
        """Test that _validate_token returns True for valid tokens."""
        from apps.core.auth import _validate_token
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        result = _validate_token('valid_token_123')
        self.assertTrue(result)
        
        # Verify the API was called with correct parameters
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertIn('test.com/api/channels/channels/', call_args[0][0])
        self.assertIn('Authorization', call_args[1]['headers'])
        self.assertEqual(call_args[1]['headers']['Authorization'], 'Bearer valid_token_123')
    
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_validate_token_with_invalid_token(self, mock_base_url, mock_get):
        """Test that _validate_token returns False for invalid tokens."""
        from apps.core.auth import _validate_token
        
        # Mock failed API response
        mock_response = Mock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response
        
        result = _validate_token('invalid_token')
        self.assertFalse(result)
    
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_validate_token_with_connection_error(self, mock_base_url, mock_get):
        """Test that _validate_token returns False on connection error."""
        from apps.core.auth import _validate_token
        
        # Mock connection error
        mock_get.side_effect = Exception("Connection failed")
        
        result = _validate_token('some_token')
        self.assertFalse(result)
    
    @patch('apps.core.auth._login')
    @patch('apps.core.auth.os.getenv')
    def test_get_auth_headers_uses_existing_token(self, mock_getenv, mock_login):
        """Test that _get_auth_headers uses existing token without validating or logging in."""
        from apps.core.auth import _get_auth_headers
        
        # Mock that we have a token
        mock_getenv.return_value = 'existing_token_123'
        
        headers = _get_auth_headers()
        
        # Verify token is used directly
        self.assertEqual(headers['Authorization'], 'Bearer existing_token_123')
        
        # Verify login was NOT called (token validation only happens on 401)
        mock_login.assert_not_called()
    
    @patch('apps.core.auth._login')
    @patch('apps.core.auth.load_dotenv')
    @patch('apps.core.auth.env_path')
    @patch('apps.core.auth.os.getenv')
    def test_get_auth_headers_logs_in_when_no_token(self, mock_getenv, mock_env_path, 
                                                       mock_load_dotenv, mock_login):
        """Test that _get_auth_headers logs in only when no token exists."""
        from apps.core.auth import _get_auth_headers
        
        # Mock environment: first call has no token, second call (after login) has new token
        token_calls = [None, None, 'new_valid_token']
        mock_getenv.side_effect = token_calls
        
        # Mock successful login
        mock_login.return_value = True
        
        # Mock that .env file exists
        mock_env_path.exists.return_value = True
        
        headers = _get_auth_headers()
        
        # Verify login WAS called because token was missing
        mock_login.assert_called_once()
        
        # Verify new token is used
        self.assertEqual(headers['Authorization'], 'Bearer new_valid_token')


class TestTokenValidationCaching(unittest.TestCase):
    """Test token validation caching functionality to reduce API calls."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Clear the token validation cache before each test
        import apps.core.auth
        apps.core.auth._token_validation_cache.clear()
        
    def tearDown(self):
        """Clean up after tests."""
        # Clear the token validation cache after each test
        import apps.core.auth
        apps.core.auth._token_validation_cache.clear()
    
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_token_validation_cache_prevents_duplicate_api_calls(self, mock_base_url, mock_get):
        """Test that cached token validation prevents redundant API calls."""
        from apps.core.auth import _validate_token, _token_validation_cache
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        # First call should make an API request
        result1 = _validate_token('valid_token_123')
        self.assertTrue(result1)
        self.assertEqual(mock_get.call_count, 1)
        
        # Second call should use cache (no additional API request)
        result2 = _validate_token('valid_token_123')
        self.assertTrue(result2)
        self.assertEqual(mock_get.call_count, 1)  # Still only 1 call
        
        # Third call should also use cache
        result3 = _validate_token('valid_token_123')
        self.assertTrue(result3)
        self.assertEqual(mock_get.call_count, 1)  # Still only 1 call
    
    @patch('apps.core.auth.time.time')
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_token_validation_cache_expires(self, mock_base_url, mock_get, mock_time):
        """Test that token validation cache expires after TTL."""
        from apps.core.auth import _validate_token, _token_validation_cache, TOKEN_VALIDATION_TTL
        
        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        # First call at time 0
        mock_time.return_value = 0
        result1 = _validate_token('valid_token_123')
        self.assertTrue(result1)
        self.assertEqual(mock_get.call_count, 1)
        
        # Second call at time 30 (within TTL) - should use cache
        mock_time.return_value = 30
        result2 = _validate_token('valid_token_123')
        self.assertTrue(result2)
        self.assertEqual(mock_get.call_count, 1)
        
        # Third call at time 61 (after TTL) - should make new API call
        mock_time.return_value = 61
        result3 = _validate_token('valid_token_123')
        self.assertTrue(result3)
        self.assertEqual(mock_get.call_count, 2)
    
    @patch('apps.core.auth.time.time')
    @patch('apps.core.auth.requests.get')
    @patch('apps.core.auth._get_base_url', return_value='http://test.com')
    def test_failed_validation_clears_cache(self, mock_base_url, mock_get, mock_time):
        """Test that failed validation clears the cache."""
        from apps.core.auth import _validate_token, _token_validation_cache
        
        # First call at time 0 - successful
        mock_time.return_value = 0
        mock_response_success = Mock()
        mock_response_success.status_code = 200
        mock_get.return_value = mock_response_success
        
        result1 = _validate_token('valid_token_123')
        self.assertTrue(result1)
        self.assertIn('bearer:valid_token_123', _token_validation_cache)
        
        # Second call at time 61 (after TTL) - failed (401)
        mock_time.return_value = 61
        mock_response_fail = Mock()
        mock_response_fail.status_code = 401
        mock_get.return_value = mock_response_fail
        
        result2 = _validate_token('valid_token_123')
        self.assertFalse(result2)
        # Cache should be cleared on failure
        self.assertNotIn('bearer:valid_token_123', _token_validation_cache)
    
    def test_clear_token_validation_cache(self):
        """Test that _clear_token_validation_cache clears all cached tokens."""
        from apps.core.auth import _clear_token_validation_cache, _token_validation_cache
        
        # Add some tokens to cache
        _token_validation_cache['token1'] = 100
        _token_validation_cache['token2'] = 200
        
        self.assertEqual(len(_token_validation_cache), 2)
        
        # Clear cache
        _clear_token_validation_cache()
        
        self.assertEqual(len(_token_validation_cache), 0)


class TestProgressTracking(unittest.TestCase):
    """Test detailed progress tracking functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.progress_file = Path(self.temp_dir) / 'test_progress.json'
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_progress_update_with_steps(self):
        """Test that progress update includes step information."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            from apps.stream.stream_checker_service import StreamCheckerProgress
            
            progress = StreamCheckerProgress(self.progress_file)
            
            # Update with step information
            progress.update(
                channel_id=1,
                channel_name='Test Channel',
                current=5,
                total=10,
                current_stream='Stream 5',
                status='analyzing',
                step='Analyzing stream quality',
                step_detail='Checking bitrate, resolution, codec (5/10)'
            )
            
            # Read back the progress
            progress_data = progress.get()
            
            # Verify all fields are present
            self.assertEqual(progress_data['channel_id'], 1)
            self.assertEqual(progress_data['channel_name'], 'Test Channel')
            self.assertEqual(progress_data['current_stream'], 5)
            self.assertEqual(progress_data['total_streams'], 10)
            self.assertEqual(progress_data['percentage'], 50.0)
            self.assertEqual(progress_data['current_stream_name'], 'Stream 5')
            self.assertEqual(progress_data['status'], 'analyzing')
            self.assertEqual(progress_data['step'], 'Analyzing stream quality')
            self.assertEqual(progress_data['step_detail'], 'Checking bitrate, resolution, codec (5/10)')
    
    def test_progress_update_without_steps(self):
        """Test that progress update works without step information (backward compatibility)."""
        with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
            from apps.stream.stream_checker_service import StreamCheckerProgress
            
            progress = StreamCheckerProgress(self.progress_file)
            
            # Update without step information
            progress.update(
                channel_id=1,
                channel_name='Test Channel',
                current=5,
                total=10,
                current_stream='Stream 5',
                status='checking'
            )
            
            # Read back the progress
            progress_data = progress.get()
            
            # Verify basic fields are present
            self.assertEqual(progress_data['channel_id'], 1)
            self.assertEqual(progress_data['status'], 'checking')
            # Step fields should be empty strings
            self.assertEqual(progress_data['step'], '')
            self.assertEqual(progress_data['step_detail'], '')


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

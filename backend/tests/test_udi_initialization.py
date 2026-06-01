#!/usr/bin/env python3
"""
Integration tests for UDI initialization endpoint.

This module tests:
- UDI initialization endpoint requires configured credentials
- UDI initialization endpoint successfully initializes UDI Manager
- UDI Manager does not auto-initialize when credentials are not configured
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_api
from web_api import app
from apps.udi import get_udi_manager


class TestUDIInitialization(unittest.TestCase):
    """Test UDI initialization endpoint and auto-initialization behavior."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.app = app.test_client()
        self.app.testing = True

        for key in (
            'DISPATCHARR_BASE_URL',
            'DISPATCHARR_URL',
            'DISPATCHARR_USER',
            'DISPATCHARR_PASS',
            'DISPATCHARR_API_KEY',
            'DISPATCHARR_TOKEN',
        ):
            os.environ.pop(key, None)
        
        # Reset singleton state that may have been created by earlier tests.
        import apps.config.dispatcharr_config as dispatcharr_config_module
        import apps.udi.manager as udi_manager_module
        dispatcharr_config_module._dispatcharr_config = None
        udi_manager_module._udi_manager = None
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        import apps.config.dispatcharr_config as dispatcharr_config_module
        import apps.udi.manager as udi_manager_module
        dispatcharr_config_module._dispatcharr_config = None
        udi_manager_module._udi_manager = None
    
    def test_initialize_udi_requires_credentials(self):
        """Test that UDI initialization endpoint requires configured credentials."""
        with patch('dispatcharr_config.CONFIG_DIR', Path(self.temp_dir)):
            # Don't create config file - credentials not configured
            response = self.app.post('/api/dispatcharr/initialize-udi')
            
            self.assertEqual(response.status_code, 400)
            data = json.loads(response.data)
            self.assertFalse(data.get('success'))
            self.assertIn('not fully configured', data.get('error', ''))
    
    def test_initialize_udi_with_credentials(self):
        """Test that UDI initialization endpoint works with configured credentials."""
        with patch('dispatcharr_config.CONFIG_DIR', Path(self.temp_dir)):
            # Create config file with credentials
            config_file = Path(self.temp_dir) / 'dispatcharr_config.json'
            config_file.write_text(json.dumps({
                'base_url': 'http://test.local',
                'username': 'testuser',
                'password': 'testpass'
            }))
            
            # Mock the UDI Manager initialization
            with patch('web_api.get_udi_manager') as mock_get_udi:
                mock_udi = MagicMock()
                mock_udi.initialize.return_value = True
                mock_udi.get_channels.return_value = [{'id': 1, 'name': 'Test'}]
                mock_udi.get_streams.return_value = []
                mock_udi.get_m3u_accounts.return_value = []
                mock_get_udi.return_value = mock_udi
                
                response = self.app.post('/api/dispatcharr/initialize-udi')
                
                self.assertEqual(response.status_code, 200)
                data = json.loads(response.data)
                self.assertTrue(data.get('success'))
                self.assertEqual(data['data']['channels_count'], 1)
                
                # Verify initialize was called with force_refresh=True
                mock_udi.initialize.assert_called_once_with(force_refresh=True)
    
    def test_udi_manager_no_auto_init_without_credentials(self):
        """Test that UDI Manager does not auto-initialize when credentials are not configured."""
        with patch('dispatcharr_config.CONFIG_DIR', Path(self.temp_dir)):
            with patch('udi.storage.CONFIG_DIR', Path(self.temp_dir)):
                # Don't create config file - credentials not configured
                udi = get_udi_manager()
                
                # Try to get channels - should not auto-initialize
                channels = udi.get_channels()
                
                # Should return empty list without initializing
                self.assertEqual(channels, [])
                self.assertFalse(udi.is_initialized())
    
    def test_udi_manager_auto_init_with_credentials(self):
        """Test that UDI Manager auto-initializes when credentials are configured."""
        with patch('dispatcharr_config.CONFIG_DIR', Path(self.temp_dir)):
            with patch('udi.storage.CONFIG_DIR', Path(self.temp_dir)):
                # Create config file with credentials
                config_file = Path(self.temp_dir) / 'dispatcharr_config.json'
                config_file.write_text(json.dumps({
                    'base_url': 'http://test.local',
                    'username': 'testuser',
                    'password': 'testpass'
                }))
                
                # Create UDI storage file to simulate existing data
                udi_file = Path(self.temp_dir) / 'udi_data.json'
                udi_file.write_text(json.dumps({
                    'channels': [{'id': 1, 'name': 'Test'}],
                    'streams': [],
                    'channel_groups': [],
                    'logos': [],
                    'm3u_accounts': []
                }))
                
                udi = get_udi_manager()
                
                # Try to get channels - should auto-initialize from storage
                channels = udi.get_channels()
                
                # Should have loaded from storage
                self.assertEqual(len(channels), 1)
                self.assertTrue(udi.is_initialized())


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Test the ensure-config endpoint for setup wizard.

This test verifies that the /api/setup-wizard/ensure-config endpoint
correctly creates empty configuration files when they don't exist.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Set up test environment before importing modules
os.environ['CONFIG_DIR'] = tempfile.mkdtemp()
os.environ['TEST_MODE'] = 'true'
os.environ['DISPATCHARR_BASE_URL'] = 'http://test-dispatcharr:8000'
os.environ['DISPATCHARR_USER'] = 'test_user'
os.environ['DISPATCHARR_PASS'] = 'test_pass'

# Import after environment is set
import web_api


class TestWizardEnsureConfig(unittest.TestCase):
    """Test cases for wizard ensure-config endpoint."""
    
    def setUp(self):
        """Set up test environment before each test."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.temp_dir
        
        # Reload web_api.CONFIG_DIR to use temp directory
        web_api.CONFIG_DIR = Path(self.temp_dir)
        
        # Create Flask test client
        web_api.app.config['TESTING'] = True
        self.client = web_api.app.test_client()
    
    def tearDown(self):
        """Clean up after each test."""
        # Clean up temp directory
        import shutil
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
    
    def test_ensure_config_creates_empty_regex_file(self):
        """Test that ensure-config creates an empty regex config file."""
        regex_file = Path(self.temp_dir) / 'channel_regex_config.json'
        
        # Verify file doesn't exist initially
        self.assertFalse(regex_file.exists(), "Regex config should not exist initially")
        
        # Call ensure-config endpoint
        response = self.client.post('/api/setup-wizard/ensure-config')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        self.assertEqual(data['message'], 'Configuration files ensured')
        
        # Verify file was created
        self.assertTrue(regex_file.exists(), "Regex config should exist after ensure-config")
        
        # Verify file contents
        with open(regex_file, 'r') as f:
            config = json.load(f)
        
        self.assertIn('patterns', config)
        self.assertIn('global_settings', config)
        self.assertEqual(config['patterns'], {})
        self.assertEqual(config['global_settings']['case_sensitive'], True)
        self.assertEqual(config['global_settings']['require_exact_match'], False)
    
    def test_ensure_config_preserves_existing_file(self):
        """Test that ensure-config doesn't overwrite existing config."""
        regex_file = Path(self.temp_dir) / 'channel_regex_config.json'
        
        # Create existing config with patterns
        existing_config = {
            "patterns": {
                "1": {
                    "name": "Test Channel",
                    "regex": ["Test.*"],
                    "enabled": True
                }
            },
            "global_settings": {
                "case_sensitive": True,
                "require_exact_match": True
            }
        }
        
        regex_file.parent.mkdir(parents=True, exist_ok=True)
        with open(regex_file, 'w') as f:
            json.dump(existing_config, f, indent=2)
        
        # Call ensure-config endpoint
        response = self.client.post('/api/setup-wizard/ensure-config')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        
        # Verify file contents are unchanged
        with open(regex_file, 'r') as f:
            config = json.load(f)
        
        self.assertEqual(config, existing_config, "Existing config should not be modified")
    
    def test_wizard_complete_with_empty_patterns(self):
        """Test that wizard can complete with empty pattern file."""
        # Create automation config
        automation_file = Path(self.temp_dir) / 'automation_config.json'
        automation_config = {
            'enabled': False,
            'playlist_refresh_interval': 300
        }
        automation_file.parent.mkdir(parents=True, exist_ok=True)
        with open(automation_file, 'w') as f:
            json.dump(automation_config, f, indent=2)
        
        # Create empty regex config via ensure-config
        response = self.client.post('/api/setup-wizard/ensure-config')
        self.assertEqual(response.status_code, 200)
        
        # Mock UDI and dispatcharr config at web_api level
        with patch('web_api.get_udi_manager') as mock_udi, \
             patch('web_api.get_dispatcharr_config') as mock_config, \
             patch('web_api.CONFIG_DIR', Path(self.temp_dir)):
            
            mock_udi_instance = mock_udi.return_value
            mock_udi_instance.get_channels.return_value = [
                {'id': 1, 'name': 'Test Channel'}
            ]
            
            mock_config_instance = mock_config.return_value
            mock_config_instance.is_configured.return_value = True
            
            # Check wizard status
            response = self.client.get('/api/setup-wizard')
            self.assertEqual(response.status_code, 200)
            
            data = json.loads(response.data)
            
            # Verify wizard sees both config files exist
            self.assertTrue(data['automation_config_exists'])
            self.assertTrue(data['regex_config_exists'])
            self.assertTrue(data['has_channels'])
            self.assertTrue(data['dispatcharr_connection'])
            
            # Verify wizard is complete even with no patterns
            self.assertTrue(data['setup_complete'], 
                "Wizard should be complete with empty pattern file")
    
    def test_ensure_config_creates_directory(self):
        """Test that ensure-config creates config directory if needed."""
        # Use a nested temp directory that doesn't exist
        nested_dir = Path(self.temp_dir) / 'nested' / 'config'
        os.environ['CONFIG_DIR'] = str(nested_dir)
        web_api.CONFIG_DIR = nested_dir
        
        # Verify directory doesn't exist
        self.assertFalse(nested_dir.exists(), "Nested directory should not exist initially")
        
        # Call ensure-config endpoint
        response = self.client.post('/api/setup-wizard/ensure-config')
        
        # Verify response
        self.assertEqual(response.status_code, 200)
        
        # Verify directory and file were created
        self.assertTrue(nested_dir.exists(), "Config directory should be created")
        regex_file = nested_dir / 'channel_regex_config.json'
        self.assertTrue(regex_file.exists(), "Regex config should be created")


if __name__ == '__main__':
    unittest.main()

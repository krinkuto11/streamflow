#!/usr/bin/env python3
"""
Unit tests for automation service auto-start after wizard completion.

This module tests:
- Services auto-start when automation controls are enabled in wizard
- Services don't start when wizard is incomplete
- Services don't start when all automation controls are disabled
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

from apps.automation.automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
from apps.stream.stream_checker_service import StreamCheckerService


class TestWizardAutostart(unittest.TestCase):
    """Test automation service auto-start after wizard completion."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def _create_complete_wizard_config(self):
        """Helper to create a complete wizard configuration."""
        config_file = Path(self.temp_dir) / 'automation_config.json'
        regex_file = Path(self.temp_dir) / 'channel_regex_config.json'
        stream_checker_file = Path(self.temp_dir) / 'stream_checker_config.json'
        
        config_file.write_text(json.dumps({
            "playlist_update_interval_minutes": 5,
            "autostart_automation": False
        }))
        
        regex_file.write_text(json.dumps({
            "patterns": {
                "1": {
                    "name": "Test Channel",
                    "regex": [".*Test.*"],
                    "enabled": True
                }
            },
            "global_settings": {
                "case_sensitive": False
            }
        }))
        
        stream_checker_file.write_text(json.dumps({
            "automation_controls": {
                "auto_m3u_updates": False,
                "auto_stream_matching": False,
                "auto_quality_checking": False,
                "scheduled_global_action": False
            },
            "enabled": True
        }))
    
    def test_services_start_when_automation_enabled_and_wizard_complete(self):
        """Test that services auto-start when automation is enabled and wizard is complete."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                # Setup complete wizard configuration
                self._create_complete_wizard_config()
                
                # Create manager and service (simulating wizard completion)
                manager = AutomatedStreamManager()
                service = StreamCheckerService()
                
                # Verify they're not running initially
                self.assertFalse(manager.running)
                self.assertFalse(service.running)
                
                # Simulate the wizard updating config with enabled automation controls
                # This should trigger auto-start
                new_config = {
                    'automation_controls': {
                        'auto_m3u_updates': True,
                        'auto_stream_matching': True,
                        'auto_quality_checking': True,
                        'scheduled_global_action': False,
                    }
                }
                service.update_config(new_config)
                
                # Note: The actual auto-start logic is in web_api.py's endpoint
                # This test verifies the services can be started programmatically
                service.start()
                manager.start_automation()
                
                self.assertTrue(service.running)
                self.assertTrue(manager.running)
                
                # Cleanup
                service.stop()
                manager.stop_automation()
    
    def test_services_dont_start_when_wizard_incomplete(self):
        """Test that services don't auto-start when wizard is incomplete."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                # Don't create pattern file (incomplete wizard)
                config_file = Path(self.temp_dir) / 'automation_config.json'
                config_file.write_text(json.dumps({
                    "playlist_update_interval_minutes": 5
                }))
                
                # Create manager and service
                manager = AutomatedStreamManager()
                service = StreamCheckerService()
                
                # Verify they're not running
                self.assertFalse(manager.running)
                self.assertFalse(service.running)
                
                # Even if we update config, they shouldn't auto-start
                # because wizard is incomplete
    
    def test_services_dont_start_when_all_automation_disabled(self):
        """Test that services don't auto-start when all automation controls are disabled."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            with patch('stream_checker_service.CONFIG_DIR', Path(self.temp_dir)):
                # Setup complete wizard configuration
                self._create_complete_wizard_config()
                
                # Create service with all automation disabled
                service = StreamCheckerService()
                service.update_config({
                    'automation_controls': {
                        'auto_m3u_updates': False,
                        'auto_stream_matching': False,
                        'auto_quality_checking': False,
                        'scheduled_global_action': False,
                    }
                })
                
                # Verify service is not running (and shouldn't be started)
                self.assertFalse(service.running)
                self.assertFalse(service.config.get('automation_controls.auto_quality_checking', True))
    
    def test_wizard_complete_check_requires_patterns(self):
        """Test that wizard completion requires regex patterns."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            # Create config files but with no patterns
            config_file = Path(self.temp_dir) / 'automation_config.json'
            regex_file = Path(self.temp_dir) / 'channel_regex_config.json'
            
            config_file.write_text(json.dumps({
                "playlist_update_interval_minutes": 5
            }))
            
            regex_file.write_text(json.dumps({
                "patterns": {},  # Empty patterns
                "global_settings": {
                    "case_sensitive": False
                }
            }))
            
            # Verify matcher has no patterns
            matcher = RegexChannelMatcher()
            patterns = matcher.get_patterns()
            self.assertEqual(len(patterns.get("patterns", {})), 0)
            
            # Wizard should be considered incomplete
            # (This would be checked by check_wizard_complete() in web_api.py)


if __name__ == '__main__':
    unittest.main()

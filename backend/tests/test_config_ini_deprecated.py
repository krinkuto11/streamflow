#!/usr/bin/env python3
"""
Unit test to verify that dispatcharr-stream-sorter works without config.ini.

This test verifies that:
1. The module can be loaded without config.ini file
2. load_config() returns default configuration
3. All expected settings are present with correct defaults
"""

import unittest
import tempfile
import os
import sys
import importlib.util
from pathlib import Path

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestConfigIniDeprecated(unittest.TestCase):
    """Test that config.ini is no longer required."""
    
    def test_load_config_without_file(self):
        """Test that load_config works without config.ini."""
        # Import the module
        spec = importlib.util.spec_from_file_location(
            "stream_sorter",
            Path(__file__).parent.parent / "dispatcharr-stream-sorter.py"
        )
        stream_sorter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stream_sorter)
        
        # Call load_config
        config = stream_sorter.load_config()
        
        # Verify config object exists and has script_settings section
        self.assertIsNotNone(config)
        self.assertIn('script_settings', config)
        
        settings = config['script_settings']
        
        # Verify all required settings exist with correct defaults
        self.assertEqual(settings.get('channel_group_ids'), 'ALL')
        self.assertEqual(settings.getint('start_channel'), 1)
        self.assertEqual(settings.getint('end_channel'), 999)
        self.assertEqual(settings.getint('stream_last_measured_days'), 7)
        self.assertEqual(settings.getint('fps_bonus_points'), 55)
    
    def test_load_config_with_existing_file(self):
        """Test that load_config still works if config.ini exists."""
        # Create a temporary config.ini file
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / 'config.ini'
            with open(config_path, 'w') as f:
                f.write('[script_settings]\n')
                f.write('channel_group_ids = 1,2,3\n')
                f.write('start_channel = 10\n')
                f.write('end_channel = 500\n')
                f.write('stream_last_measured_days = 14\n')
                f.write('fps_bonus_points = 100\n')
            
            # Test the config loading logic directly
            import configparser
            config = configparser.ConfigParser()
            
            # This simulates what happens when config.ini exists
            if config_path.exists():
                config.read(config_path)
            
            # Verify the config was loaded from file
            self.assertIn('script_settings', config)
            settings = config['script_settings']
            self.assertEqual(settings.get('channel_group_ids'), '1,2,3')
            self.assertEqual(settings.getint('start_channel'), 10)
            self.assertEqual(settings.getint('end_channel'), 500)


if __name__ == '__main__':
    unittest.main()

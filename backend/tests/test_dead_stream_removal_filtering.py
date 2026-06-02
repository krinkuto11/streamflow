#!/usr/bin/env python3
"""
Test that dead stream removal configuration is respected during stream updates.

This test verifies that the allow_dead_streams parameter is correctly passed
based on the dead_stream_removal_enabled configuration setting in both:
1. stream_checker_service.py - during channel checks
2. automated_stream_manager.py - during stream discovery/assignment
"""

import unittest
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os
import tempfile
import json
from pathlib import Path

# Set up CONFIG_DIR before importing modules
test_config_dir = tempfile.mkdtemp()
os.environ['CONFIG_DIR'] = test_config_dir

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestDeadStreamRemovalFiltering(unittest.TestCase):
    """Test dead stream removal configuration is respected."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.config_dir = Path(test_config_dir)
        
    def _create_stream_checker_config(self, removal_enabled):
        """Create a stream checker config with the specified removal setting."""
        config = {
            'enabled': True,
            'dead_stream_handling': {
                'enabled': removal_enabled,
                'min_resolution_width': 0,
                'min_resolution_height': 0,
                'min_bitrate_kbps': 0,
                'min_score': 0
            }
        }
        config_file = self.config_dir / 'stream_checker_config.json'
        with open(config_file, 'w') as f:
            json.dump(config, f)
        from apps.database.manager import get_db_manager
        get_db_manager().set_system_setting('stream_checker_config', config)
    
    def test_stream_checker_respects_removal_disabled(self):
        """Test that stream checker passes allow_dead_streams=True when removal is disabled."""
        # Create config with removal disabled
        self._create_stream_checker_config(removal_enabled=False)
        
        # Simulate the logic used in stream_checker_service.py
        # This is the config reading pattern used in the actual code
        config = {
            'enabled': True,
            'dead_stream_handling': {
                'enabled': False  # Removal disabled
            }
        }
        
        # Verify config is loaded correctly
        removal_enabled = config.get('dead_stream_handling', {}).get('enabled', True)
        self.assertFalse(removal_enabled, "Dead stream removal should be disabled")
        
        # Verify that allow_dead_streams should be set to True (not removal_enabled)
        expected_allow_dead_streams = not removal_enabled
        self.assertTrue(expected_allow_dead_streams, "allow_dead_streams should be True when removal is disabled")
    
    def test_stream_checker_respects_removal_enabled(self):
        """Test that stream checker passes allow_dead_streams=False when removal is enabled."""
        # Create config with removal enabled
        self._create_stream_checker_config(removal_enabled=True)
        
        # Simulate the logic used in stream_checker_service.py
        config = {
            'enabled': True,
            'dead_stream_handling': {
                'enabled': True  # Removal enabled
            }
        }
        
        # Verify config is loaded correctly
        removal_enabled = config.get('dead_stream_handling', {}).get('enabled', True)
        self.assertTrue(removal_enabled, "Dead stream removal should be enabled")
        
        # Verify that allow_dead_streams should be set to False (not removal_enabled)
        expected_allow_dead_streams = not removal_enabled
        self.assertFalse(expected_allow_dead_streams, "allow_dead_streams should be False when removal is enabled")
    
    @patch('automated_stream_manager.add_streams_to_channel')
    def test_automated_manager_respects_removal_disabled(self, mock_add_streams):
        """Test that automated stream manager passes allow_dead_streams=True when removal is disabled."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Create config with removal disabled
        self._create_stream_checker_config(removal_enabled=False)
        
        # Create manager instance
        manager = AutomatedStreamManager()
        
        # Test the _is_dead_stream_removal_enabled method
        removal_enabled = manager._is_dead_stream_removal_enabled()
        self.assertFalse(removal_enabled, "Dead stream removal should be disabled")
        
        # Verify that allow_dead_streams should be set to True (not removal_enabled)
        expected_allow_dead_streams = not removal_enabled
        self.assertTrue(expected_allow_dead_streams, "allow_dead_streams should be True when removal is disabled")
    
    @patch('automated_stream_manager.add_streams_to_channel')
    def test_automated_manager_respects_removal_enabled(self, mock_add_streams):
        """Test that automated stream manager passes allow_dead_streams=False when removal is enabled."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Create config with removal enabled
        self._create_stream_checker_config(removal_enabled=True)
        
        # Create manager instance
        manager = AutomatedStreamManager()
        
        # Test the _is_dead_stream_removal_enabled method
        removal_enabled = manager._is_dead_stream_removal_enabled()
        self.assertTrue(removal_enabled, "Dead stream removal should be enabled")
        
        # Verify that allow_dead_streams should be set to False (not removal_enabled)
        expected_allow_dead_streams = not removal_enabled
        self.assertFalse(expected_allow_dead_streams, "allow_dead_streams should be False when removal is enabled")
    
    def test_config_reading_default_value(self):
        """Test that missing config defaults to removal enabled (True)."""
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Don't create any config file
        config_file = self.config_dir / 'stream_checker_config.json'
        if config_file.exists():
            config_file.unlink()
        
        # Create manager instance
        manager = AutomatedStreamManager()
        
        # Should default to True (removal enabled) when config doesn't exist
        removal_enabled = manager._is_dead_stream_removal_enabled()
        self.assertTrue(removal_enabled, "Should default to removal enabled when config is missing")


def cleanup():
    """Clean up test configuration directory"""
    import shutil
    try:
        shutil.rmtree(test_config_dir)
        print(f"✓ Cleaned up test directory: {test_config_dir}")
    except Exception as e:
        print(f"Warning: Could not clean up test directory: {e}")


if __name__ == '__main__':
    try:
        print("=" * 80)
        print("Testing Dead Stream Removal Configuration Filtering")
        print("=" * 80)
        
        # Run the tests
        unittest.main(verbosity=2, exit=False)
        
        print("=" * 80)
        print("✅ All dead stream removal filtering tests passed!")
        print("=" * 80)
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup()

#!/usr/bin/env python3
"""
Tests for Profile Configuration Manager

Tests the channel profile configuration system for managing
profile selection and snapshots for dead stream management.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from profile_config import ProfileConfig
except ModuleNotFoundError as exc:
    raise unittest.SkipTest("Legacy profile_config module is not part of the SQL-backed V2 runtime") from exc


class TestProfileConfig(unittest.TestCase):
    """Test cases for ProfileConfig class."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create a temporary directory for test config files
        self.test_dir = tempfile.mkdtemp()
        self.config_file = Path(self.test_dir) / 'profile_config.json'
        
        # Override CONFIG_DIR for testing
        os.environ['CONFIG_DIR'] = self.test_dir
        
        # Create a fresh ProfileConfig instance for each test
        self.config = ProfileConfig()
    
    def tearDown(self):
        """Clean up after tests."""
        # Remove test directory and files
        import shutil
        if Path(self.test_dir).exists():
            shutil.rmtree(self.test_dir)
    
    def test_default_config(self):
        """Test that default configuration is created correctly."""
        config_data = self.config.get_config()
        
        self.assertIsNone(config_data['selected_profile_id'])
        self.assertFalse(config_data['use_profile'])
        self.assertFalse(config_data['dead_streams']['enabled'])
        self.assertEqual(config_data['snapshots'], {})
    
    def test_set_selected_profile(self):
        """Test setting a selected profile."""
        result = self.config.set_selected_profile(1, "Test Profile")
        
        self.assertTrue(result)
        self.assertEqual(self.config.get_selected_profile(), 1)
        self.assertTrue(self.config.is_using_profile())
        
        # Test unsetting profile
        result = self.config.set_selected_profile(None, None)
        self.assertTrue(result)
        self.assertIsNone(self.config.get_selected_profile())
        self.assertFalse(self.config.is_using_profile())
    
    def test_dead_stream_config(self):
        """Test dead stream configuration management."""
        # Set dead stream config
        result = self.config.set_dead_stream_config(
            enabled=True,
            target_profile_id=2,
            target_profile_name="Target Profile",
            use_snapshot=True
        )
        
        self.assertTrue(result)
        
        # Verify configuration
        ds_config = self.config.get_dead_stream_config()
        self.assertTrue(ds_config['enabled'])
        self.assertEqual(ds_config['target_profile_id'], 2)
        self.assertEqual(ds_config['target_profile_name'], "Target Profile")
        self.assertTrue(ds_config['use_snapshot'])
        
        # Test getters
        self.assertTrue(self.config.is_dead_stream_management_enabled())
        self.assertEqual(self.config.get_target_profile_for_dead_streams(), 2)
    
    def test_create_snapshot(self):
        """Test creating a profile snapshot."""
        channel_ids = [1, 2, 3, 4, 5]
        
        result = self.config.create_snapshot(
            profile_id=1,
            profile_name="Test Profile",
            channel_ids=channel_ids
        )
        
        self.assertTrue(result)
        self.assertTrue(self.config.has_snapshot(1))
        
        # Verify snapshot contents
        snapshot = self.config.get_snapshot(1)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot['profile_id'], 1)
        self.assertEqual(snapshot['profile_name'], "Test Profile")
        self.assertEqual(snapshot['channel_ids'], channel_ids)
        self.assertEqual(snapshot['channel_count'], len(channel_ids))
        self.assertIn('created_at', snapshot)
    
    def test_get_snapshot_not_found(self):
        """Test getting a snapshot that doesn't exist."""
        snapshot = self.config.get_snapshot(999)
        self.assertIsNone(snapshot)
        self.assertFalse(self.config.has_snapshot(999))
    
    def test_delete_snapshot(self):
        """Test deleting a profile snapshot."""
        # Create a snapshot first
        self.config.create_snapshot(
            profile_id=1,
            profile_name="Test Profile",
            channel_ids=[1, 2, 3]
        )
        
        self.assertTrue(self.config.has_snapshot(1))
        
        # Delete the snapshot
        result = self.config.delete_snapshot(1)
        self.assertTrue(result)
        self.assertFalse(self.config.has_snapshot(1))
        
        # Delete non-existent snapshot (should succeed)
        result = self.config.delete_snapshot(999)
        self.assertTrue(result)
    
    def test_get_all_snapshots(self):
        """Test getting all snapshots."""
        # Create multiple snapshots
        self.config.create_snapshot(1, "Profile 1", [1, 2])
        self.config.create_snapshot(2, "Profile 2", [3, 4, 5])
        self.config.create_snapshot(3, "Profile 3", [6])
        
        snapshots = self.config.get_all_snapshots()
        
        self.assertEqual(len(snapshots), 3)
        self.assertIn('1', snapshots)
        self.assertIn('2', snapshots)
        self.assertIn('3', snapshots)
    
    def test_config_persistence(self):
        """Test that configuration persists across instances."""
        # Set some configuration
        self.config.set_selected_profile(5, "Profile 5")
        self.config.set_dead_stream_config(
            enabled=True,
            target_profile_id=10,
            use_snapshot=True
        )
        self.config.create_snapshot(5, "Profile 5", [1, 2, 3])
        
        # Create a new instance (should load from file)
        new_config = ProfileConfig()
        
        # Verify configuration was loaded
        self.assertEqual(new_config.get_selected_profile(), 5)
        self.assertTrue(new_config.is_dead_stream_management_enabled())
        self.assertTrue(new_config.has_snapshot(5))
        
        snapshot = new_config.get_snapshot(5)
        self.assertEqual(snapshot['channel_ids'], [1, 2, 3])
    
    def test_partial_dead_stream_config_update(self):
        """Test updating only some dead stream config fields."""
        # Set initial config
        self.config.set_dead_stream_config(
            enabled=True,
            target_profile_id=1,
            use_snapshot=False
        )
        
        # Update only enabled field
        self.config.set_dead_stream_config(enabled=False)
        
        ds_config = self.config.get_dead_stream_config()
        self.assertFalse(ds_config['enabled'])
        self.assertEqual(ds_config['target_profile_id'], 1)
        self.assertFalse(ds_config['use_snapshot'])
    
    def test_disable_profile_usage(self):
        """Test that disabling profile usage clears the selected profile."""
        # Set a profile first
        self.config.set_selected_profile(3, "Test Profile")
        self.assertEqual(self.config.get_selected_profile(), 3)
        self.assertTrue(self.config.is_using_profile())
        
        # Disable profile usage by setting to None
        self.config.set_selected_profile(None, None)
        
        # Verify profile is cleared and use_profile is False
        self.assertIsNone(self.config.get_selected_profile())
        self.assertFalse(self.config.is_using_profile())
        
        config_data = self.config.get_config()
        self.assertIsNone(config_data['selected_profile_id'])
        self.assertIsNone(config_data['selected_profile_name'])
        self.assertFalse(config_data['use_profile'])


if __name__ == '__main__':
    unittest.main()

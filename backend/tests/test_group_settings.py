#!/usr/bin/env python3
"""
Tests for group settings functionality
"""

import os
import sys
import tempfile
import shutil
import importlib
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Create a temporary config directory for testing
test_config_dir = tempfile.mkdtemp()
os.environ['CONFIG_DIR'] = test_config_dir

sys.modules.pop('channel_settings_manager', None)
ChannelSettingsManager = importlib.import_module('channel_settings_manager').ChannelSettingsManager


def test_group_settings_basic():
    """Test basic group settings operations"""
    print("Testing basic group settings...")
    
    manager = ChannelSettingsManager()
    
    # Test default settings
    settings = manager.get_group_settings(1)
    assert settings['matching_mode'] == 'enabled', "Default matching_mode should be enabled"
    assert settings['checking_mode'] == 'enabled', "Default checking_mode should be enabled"
    
    # Test setting matching mode
    success = manager.set_group_settings(1, matching_mode='disabled')
    assert success, "Setting matching_mode should succeed"
    
    settings = manager.get_group_settings(1)
    assert settings['matching_mode'] == 'disabled', "Matching mode should be disabled"
    assert settings['checking_mode'] == 'enabled', "Checking mode should still be enabled"
    
    # Test setting checking mode
    success = manager.set_group_settings(1, checking_mode='disabled')
    assert success, "Setting checking_mode should succeed"
    
    settings = manager.get_group_settings(1)
    assert settings['matching_mode'] == 'disabled', "Matching mode should still be disabled"
    assert settings['checking_mode'] == 'disabled', "Checking mode should be disabled"
    
    print("✓ Basic group settings test passed")


def test_group_settings_persistence():
    """Test that group settings persist across instances"""
    print("Testing group settings persistence...")
    
    # Create first manager and set settings
    manager1 = ChannelSettingsManager()
    manager1.set_group_settings(2, matching_mode='disabled', checking_mode='disabled')
    
    # Create second manager and verify settings persist
    manager2 = ChannelSettingsManager()
    settings = manager2.get_group_settings(2)
    assert settings['matching_mode'] == 'disabled', "Settings should persist"
    assert settings['checking_mode'] == 'disabled', "Settings should persist"
    
    print("✓ Group settings persistence test passed")


def test_group_settings_all():
    """Test getting all group settings"""
    print("Testing get all group settings...")
    
    manager = ChannelSettingsManager()
    
    # Set settings for multiple groups
    manager.set_group_settings(10, matching_mode='disabled')
    manager.set_group_settings(20, checking_mode='disabled')
    manager.set_group_settings(30, matching_mode='disabled', checking_mode='disabled')
    
    # Get all settings
    all_settings = manager.get_all_group_settings()
    
    assert 10 in all_settings, "Group 10 should be in all settings"
    assert 20 in all_settings, "Group 20 should be in all settings"
    assert 30 in all_settings, "Group 30 should be in all settings"
    
    assert all_settings[10]['matching_mode'] == 'disabled'
    assert all_settings[20]['checking_mode'] == 'disabled'
    assert all_settings[30]['matching_mode'] == 'disabled'
    assert all_settings[30]['checking_mode'] == 'disabled'
    
    print("✓ Get all group settings test passed")


def test_group_checking_helpers():
    """Test group checking helper methods"""
    print("Testing group checking helpers...")
    
    manager = ChannelSettingsManager()
    
    # Test enabled group
    manager.set_group_settings(100, matching_mode='enabled', checking_mode='enabled')
    assert manager.is_group_matching_enabled(100), "Group 100 matching should be enabled"
    assert manager.is_group_checking_enabled(100), "Group 100 checking should be enabled"
    
    # Test disabled group
    manager.set_group_settings(200, matching_mode='disabled', checking_mode='disabled')
    assert not manager.is_group_matching_enabled(200), "Group 200 matching should be disabled"
    assert not manager.is_group_checking_enabled(200), "Group 200 checking should be disabled"
    
    # Test channel enabled by group
    assert manager.is_channel_enabled_by_group(100, 'matching'), "Channel in group 100 should be enabled for matching"
    assert not manager.is_channel_enabled_by_group(200, 'matching'), "Channel in group 200 should be disabled for matching"
    
    # Test channel with no group (should be enabled)
    assert manager.is_channel_enabled_by_group(None, 'matching'), "Channel with no group should be enabled"
    assert manager.is_channel_enabled_by_group(None, 'checking'), "Channel with no group should be enabled"
    
    print("✓ Group checking helpers test passed")


def cleanup():
    """Clean up test configuration directory"""
    try:
        shutil.rmtree(test_config_dir)
        print(f"✓ Cleaned up test directory: {test_config_dir}")
    except Exception as e:
        print(f"Warning: Could not clean up test directory: {e}")


if __name__ == '__main__':
    try:
        print("=" * 60)
        print("Testing Group Settings Manager")
        print("=" * 60)
        
        test_group_settings_basic()
        test_group_settings_persistence()
        test_group_settings_all()
        test_group_checking_helpers()
        
        print("=" * 60)
        print("✅ All group settings tests passed!")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup()

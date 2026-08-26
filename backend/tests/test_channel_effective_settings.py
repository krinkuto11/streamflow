#!/usr/bin/env python3
"""
Tests for channel effective settings with group inheritance
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


def test_effective_settings_channel_override():
    """Test that channel-specific settings override group settings"""
    print("Testing channel override of group settings...")
    
    manager = ChannelSettingsManager()
    
    # Set group settings
    manager.set_group_settings(1, matching_mode='disabled', checking_mode='disabled')
    
    # Set channel settings to override
    manager.set_channel_settings(100, matching_mode='enabled', checking_mode='enabled')
    
    # Get effective settings
    effective = manager.get_channel_effective_settings(100, channel_group_id=1)
    
    # Channel override should take precedence
    assert effective['matching_mode'] == 'enabled', "Channel override should be used"
    assert effective['checking_mode'] == 'enabled', "Channel override should be used"
    assert effective['matching_mode_source'] == 'channel', "Source should be channel"
    assert effective['checking_mode_source'] == 'channel', "Source should be channel"
    assert effective['has_explicit_matching'] == True, "Should have explicit matching"
    assert effective['has_explicit_checking'] == True, "Should have explicit checking"
    
    print("✓ Channel override test passed")


def test_effective_settings_group_inheritance():
    """Test that channel inherits from group when no channel-specific setting"""
    print("Testing group inheritance...")
    
    manager = ChannelSettingsManager()
    
    # Set group settings
    manager.set_group_settings(2, matching_mode='disabled', checking_mode='disabled')
    
    # Get effective settings for channel without explicit settings
    effective = manager.get_channel_effective_settings(200, channel_group_id=2)
    
    # Should inherit from group
    assert effective['matching_mode'] == 'disabled', "Should inherit from group"
    assert effective['checking_mode'] == 'disabled', "Should inherit from group"
    assert effective['matching_mode_source'] == 'group', "Source should be group"
    assert effective['checking_mode_source'] == 'group', "Source should be group"
    assert effective['has_explicit_matching'] == False, "Should not have explicit matching"
    assert effective['has_explicit_checking'] == False, "Should not have explicit checking"
    
    print("✓ Group inheritance test passed")


def test_effective_settings_no_group():
    """Test that channel uses defaults when not in a group"""
    print("Testing default settings without group...")
    
    manager = ChannelSettingsManager()
    
    # Get effective settings for channel without group or explicit settings
    effective = manager.get_channel_effective_settings(300, channel_group_id=None)
    
    # Should use defaults
    assert effective['matching_mode'] == 'enabled', "Should default to enabled"
    assert effective['checking_mode'] == 'enabled', "Should default to enabled"
    assert effective['matching_mode_source'] == 'default', "Source should be default"
    assert effective['checking_mode_source'] == 'default', "Source should be default"
    assert effective['has_explicit_matching'] == False, "Should not have explicit matching"
    assert effective['has_explicit_checking'] == False, "Should not have explicit checking"
    
    print("✓ Default settings test passed")


def test_effective_settings_partial_override():
    """Test that channel can partially override group settings"""
    print("Testing partial override...")
    
    manager = ChannelSettingsManager()
    
    # Set group settings
    manager.set_group_settings(3, matching_mode='disabled', checking_mode='disabled')
    
    # Set only matching mode for channel (checking mode inherits from group)
    manager.set_channel_settings(400, matching_mode='enabled')
    
    # Get effective settings
    effective = manager.get_channel_effective_settings(400, channel_group_id=3)
    
    # Matching should be from channel, checking should be from group
    assert effective['matching_mode'] == 'enabled', "Matching should be from channel"
    assert effective['checking_mode'] == 'disabled', "Checking should be from group"
    assert effective['matching_mode_source'] == 'channel', "Matching source should be channel"
    assert effective['checking_mode_source'] == 'group', "Checking source should be group"
    assert effective['has_explicit_matching'] == True, "Should have explicit matching"
    assert effective['has_explicit_checking'] == False, "Should not have explicit checking"
    
    print("✓ Partial override test passed")


def test_effective_settings_group_change():
    """Test that changing group settings affects channels"""
    print("Testing group setting changes...")
    
    manager = ChannelSettingsManager()
    
    # Set initial group settings
    manager.set_group_settings(4, matching_mode='enabled', checking_mode='enabled')
    
    # Get effective settings
    effective1 = manager.get_channel_effective_settings(500, channel_group_id=4)
    assert effective1['matching_mode'] == 'enabled', "Should initially be enabled"
    
    # Change group settings
    manager.set_group_settings(4, matching_mode='disabled')
    
    # Get effective settings again
    effective2 = manager.get_channel_effective_settings(500, channel_group_id=4)
    assert effective2['matching_mode'] == 'disabled', "Should reflect group change"
    assert effective2['matching_mode_source'] == 'group', "Should still be from group"
    
    print("✓ Group change test passed")


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
        print("Testing Channel Effective Settings with Group Inheritance")
        print("=" * 60)
        
        test_effective_settings_channel_override()
        test_effective_settings_group_inheritance()
        test_effective_settings_no_group()
        test_effective_settings_partial_override()
        test_effective_settings_group_change()
        
        print("=" * 60)
        print("✅ All effective settings tests passed!")
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

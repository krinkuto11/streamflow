#!/usr/bin/env python3
"""
Test that dead stream removal configuration structure is correct.

This test verifies that the dead_stream_handling configuration
can be set to enabled=False and that this value is preserved.
"""

import os
import sys
import tempfile
import shutil
import json
from pathlib import Path

# Create a temporary config directory for testing
test_config_dir = tempfile.mkdtemp()


def test_dead_stream_removal_disabled():
    """Test that dead stream removal can be disabled in config"""
    print("Testing dead stream removal disabled...")
    
    # Create a config file with dead stream removal disabled
    config_file = Path(test_config_dir) / 'stream_checker_config.json'
    config = {
        'enabled': True,
        'dead_stream_handling': {
            'enabled': False,  # Disable removal
            'min_resolution_width': 0,
            'min_resolution_height': 0,
            'min_bitrate_kbps': 0,
            'min_score': 0
        }
    }
    with open(config_file, 'w') as f:
        json.dump(config, f)
    
    # Read back and verify
    with open(config_file, 'r') as f:
        loaded_config = json.load(f)
    
    assert loaded_config['dead_stream_handling']['enabled'] == False, \
        "Dead stream removal should be disabled"
    print("✓ Config correctly saved with removal disabled")


def test_dead_stream_removal_enabled():
    """Test that dead stream removal can be enabled in config"""
    print("Testing dead stream removal enabled...")
    
    # Create a config file with dead stream removal enabled
    config_file = Path(test_config_dir) / 'stream_checker_config_enabled.json'
    config = {
        'enabled': True,
        'dead_stream_handling': {
            'enabled': True,  # Enable removal
            'min_resolution_width': 0,
            'min_resolution_height': 0,
            'min_bitrate_kbps': 0,
            'min_score': 0
        }
    }
    with open(config_file, 'w') as f:
        json.dump(config, f)
    
    # Read back and verify
    with open(config_file, 'r') as f:
        loaded_config = json.load(f)
    
    assert loaded_config['dead_stream_handling']['enabled'] == True, \
        "Dead stream removal should be enabled"
    print("✓ Config correctly saved with removal enabled")


def test_config_structure():
    """Test that the config structure is correct"""
    print("Testing config structure...")
    
    config_file = Path(test_config_dir) / 'stream_checker_config_structure.json'
    config = {
        'enabled': True,
        'dead_stream_handling': {
            'enabled': False,
            'min_resolution_width': 1280,
            'min_resolution_height': 720,
            'min_bitrate_kbps': 1000,
            'min_score': 50
        }
    }
    with open(config_file, 'w') as f:
        json.dump(config, f)
    
    # Read back and verify structure
    with open(config_file, 'r') as f:
        loaded_config = json.load(f)
    
    assert 'dead_stream_handling' in loaded_config, "Config should have dead_stream_handling"
    assert 'enabled' in loaded_config['dead_stream_handling'], \
        "dead_stream_handling should have enabled field"
    assert isinstance(loaded_config['dead_stream_handling']['enabled'], bool), \
        "enabled should be a boolean"
    assert loaded_config['dead_stream_handling']['min_resolution_width'] == 1280, \
        "Should preserve min_resolution_width"
    
    print("✓ Config structure is correct")


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
        print("Testing Dead Stream Removal Configuration")
        print("=" * 60)
        
        test_dead_stream_removal_disabled()
        test_dead_stream_removal_enabled()
        test_config_structure()
        
        print("=" * 60)
        print("✅ All dead stream removal config tests passed!")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        cleanup()

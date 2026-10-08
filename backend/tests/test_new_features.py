#!/usr/bin/env python3
"""
Tests for new features added:
1. Configurable stream startup buffer
2. Regex validation for existing streams
3. Global M3U priority mode
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_stream_startup_buffer_config():
    """Test that stream startup buffer is properly configured in defaults."""
    # Create temporary config directory first before any imports
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ['CONFIG_DIR'] = tmpdir
        
        # Now import (this will use the CONFIG_DIR)
        from apps.stream.stream_checker_service import StreamCheckConfig
        
        # Initialize config
        config = StreamCheckConfig(config_file=Path(tmpdir) / 'stream_checker_config.json')
        
        # Check that stream_startup_buffer is in default config
        assert 'stream_startup_buffer' in config.config.get('stream_analysis', {}), \
            "stream_startup_buffer should be in default config"
        assert 'bitrate_recheck_enabled' in config.config.get('stream_analysis', {}), \
            "bitrate_recheck_enabled should be in default config"
        
        # Check default value
        default_buffer = config.config['stream_analysis']['stream_startup_buffer']
        assert default_buffer == 10, \
            f"Default stream_startup_buffer should be 10, got {default_buffer}"
        assert config.config['stream_analysis']['bitrate_recheck_enabled'] is True, \
            "Default bitrate_recheck_enabled should be True"
        
        print("✓ Stream startup buffer configuration test passed")


def test_validate_existing_streams_config():
    """Test that validate_existing_streams is properly configured in defaults."""
    # Create temporary config directory first before any imports
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ['CONFIG_DIR'] = tmpdir
        config_file = Path(tmpdir) / 'automation_config.json'
        
        from apps.automation.automated_stream_manager import AutomatedStreamManager
        
        # Initialize manager
        manager = AutomatedStreamManager(config_file=config_file)
        
        # Check that validate_existing_streams is in config
        assert 'validate_existing_streams' in manager.config, \
            "validate_existing_streams should be in config"
        
        # Check default value (should be False by default)
        default_value = manager.config['validate_existing_streams']
        assert default_value is False, \
            f"Default validate_existing_streams should be False, got {default_value}"
        
        print("✓ Validate existing streams configuration test passed")


def test_global_priority_mode_config():
    """Test that global priority mode is properly configured."""
    # Create temporary config directory first before any imports
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ['CONFIG_DIR'] = tmpdir
        
        from m3u_priority_config import M3UPriorityConfig
        
        # Initialize config
        config = M3UPriorityConfig()
        
        # Check that global_priority_mode is in config
        assert 'global_priority_mode' in config._config, \
            "global_priority_mode should be in config"
        
        # Check default value
        default_mode = config.get_global_priority_mode()
        assert default_mode == 'disabled', \
            f"Default global_priority_mode should be 'disabled', got {default_mode}"
        
        # Test setting global priority mode
        valid_modes = ['disabled', 'same_resolution', 'all_streams']
        for mode in valid_modes:
            result = config.set_global_priority_mode(mode)
            assert result is True, f"Setting global_priority_mode to {mode} should succeed"
            
            retrieved_mode = config.get_global_priority_mode()
            assert retrieved_mode == mode, \
                f"Retrieved mode should be {mode}, got {retrieved_mode}"
        
        # Test invalid mode
        result = config.set_global_priority_mode('invalid_mode')
        assert result is False, "Setting invalid global_priority_mode should fail"
        
        print("✓ Global priority mode configuration test passed")


def test_stream_check_utils_signature():
    """Test that stream check utils functions have the new stream_startup_buffer parameter."""
    import inspect
    from apps.stream.stream_check_utils import analyze_stream, get_stream_info_and_bitrate
    
    # Check analyze_stream signature
    sig = inspect.signature(analyze_stream)
    assert 'stream_startup_buffer' in sig.parameters, \
        "analyze_stream should have stream_startup_buffer parameter"
    
    # Check default value
    default = sig.parameters['stream_startup_buffer'].default
    assert default == 10, \
        f"Default stream_startup_buffer should be 10, got {default}"
    
    # Check get_stream_info_and_bitrate signature
    sig = inspect.signature(get_stream_info_and_bitrate)
    assert 'stream_startup_buffer' in sig.parameters, \
        "get_stream_info_and_bitrate should have stream_startup_buffer parameter"
    
    default = sig.parameters['stream_startup_buffer'].default
    assert default == 10, \
        f"Default stream_startup_buffer should be 10, got {default}"
    
    print("✓ Stream check utils signature test passed")


if __name__ == '__main__':
    print("Running new features tests...")
    print()
    
    try:
        test_stream_startup_buffer_config()
        test_validate_existing_streams_config()
        test_global_priority_mode_config()
        test_stream_check_utils_signature()
        
        print()
        print("=" * 60)
        print("✓ All tests passed successfully!")
        print("=" * 60)
        sys.exit(0)
        
    except AssertionError as e:
        print()
        print("=" * 60)
        print(f"✗ Test failed: {e}")
        print("=" * 60)
        sys.exit(1)
    except Exception as e:
        print()
        print("=" * 60)
        print(f"✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        print("=" * 60)
        sys.exit(1)

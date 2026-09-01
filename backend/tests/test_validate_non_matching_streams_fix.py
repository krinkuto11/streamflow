#!/usr/bin/env python3
"""
Test for the fix of validate_and_remove_non_matching_streams function.

Bug Fixed: Previously, when "Remove Non-Matching Streams" was enabled, the function
would remove ALL streams from channels that didn't have regex patterns configured.
This happened because match_stream_to_channels() returns an empty list for channels
without patterns, causing all streams to be considered "non-matching".

Expected Behavior After Fix:
1. Only channels WITH regex patterns configured are validated
2. Only channels WITH matching enabled are validated  
3. Channels without regex patterns are completely skipped (no streams removed)
4. Channels with disabled regex patterns are skipped
5. Channels with matching toggle OFF are skipped

This test verifies that:
- Channels without regex patterns are NOT validated
- Only channels WITH regex patterns have their streams validated
- Streams are only removed from channels that have regex patterns configured
"""

import unittest
import tempfile
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

# Add backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

# Set minimal environment BEFORE imports
os.environ['DISPATCHARR_BASE_URL'] = 'http://test.local'
os.environ['DISPATCHARR_TOKEN'] = 'test_token'


class TestValidateNonMatchingStreamsFix(unittest.TestCase):
    """Test the fix for validate_and_remove_non_matching_streams."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create a fresh temp directory for each test
        self.test_config_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.test_config_dir
        
        # Import after setting CONFIG_DIR
        from apps.automation.automated_stream_manager import AutomatedStreamManager, RegexChannelMatcher
        import apps.automation.automated_stream_manager as asm_module
        
        # Reset CONFIG_DIR in the module
        asm_module.CONFIG_DIR = Path(self.test_config_dir)
        
        # Create automation manager instance
        self.automation_manager = AutomatedStreamManager()
        self.regex_matcher = RegexChannelMatcher()
        
        # Setup mock UDI
        self.mock_udi_patcher = patch('automated_stream_manager.get_udi_manager')
        self.mock_udi = self.mock_udi_patcher.start()
        
        # Setup mock channel settings manager
        self.mock_channel_settings_patcher = patch('automated_stream_manager.get_channel_settings_manager')
        self.mock_channel_settings = self.mock_channel_settings_patcher.start()
        
        # Setup mock stream checker
        self.mock_stream_checker_patcher = patch('automated_stream_manager.get_stream_checker_service')
        self.mock_stream_checker = self.mock_stream_checker_patcher.start()
        
        # Stream checker config no longer gates removal; removal is per-profile.
        mock_stream_checker_instance = MagicMock()
        mock_stream_checker_instance.config = {
            'automation_controls': {}
        }
        self.mock_stream_checker.return_value = mock_stream_checker_instance
        
        # Mock update_channel_streams
        self.mock_update_streams_patcher = patch('automated_stream_manager.update_channel_streams')
        self.mock_update_streams = self.mock_update_streams_patcher.start()
        self.mock_update_streams.return_value = True
    
    def tearDown(self):
        """Clean up test fixtures."""
        self.mock_udi_patcher.stop()
        self.mock_channel_settings_patcher.stop()
        self.mock_stream_checker_patcher.stop()
        self.mock_update_streams_patcher.stop()
        
        # Clean up the temp directory
        import shutil
        if os.path.exists(self.test_config_dir):
            shutil.rmtree(self.test_config_dir)
    
    def test_channels_without_regex_are_skipped(self):
        """Test that channels without regex patterns are not validated."""
        
        # Setup: Create 3 channels
        # Channel 1: Has regex patterns
        # Channel 2: No regex patterns
        # Channel 3: No regex patterns
        
        channels = [
            {'id': 1, 'name': 'Channel 1', 'channel_group_id': None},
            {'id': 2, 'name': 'Channel 2', 'channel_group_id': None},
            {'id': 3, 'name': 'Channel 3', 'channel_group_id': None}
        ]
        
        # Setup streams
        streams = [
            {'id': 101, 'name': 'News Stream 1', 'm3u_account': 1},
            {'id': 102, 'name': 'Sports Stream', 'm3u_account': 1},
            {'id': 103, 'name': 'Movie Stream', 'm3u_account': 1},
        ]
        
        # Channel 1 has streams 101, 102
        # Channel 2 has stream 103
        # Channel 3 has no streams
        channel_streams = {
            1: [{'id': 101, 'name': 'News Stream 1'}, {'id': 102, 'name': 'Sports Stream'}],
            2: [{'id': 103, 'name': 'Movie Stream'}],
            3: []
        }
        
        # Setup UDI mock
        udi_instance = MagicMock()
        udi_instance.get_channels.return_value = channels
        udi_instance.get_streams.return_value = streams
        udi_instance.get_channel_streams.side_effect = lambda ch_id: channel_streams.get(ch_id, [])
        udi_instance.refresh_channel_by_id.return_value = None
        self.mock_udi.return_value = udi_instance
        
        # Setup channel settings mock - all channels have matching enabled
        channel_settings_instance = MagicMock()
        channel_settings_instance._settings = {}
        channel_settings_instance.is_matching_enabled.return_value = True
        channel_settings_instance.is_channel_enabled_by_group.return_value = True
        channel_settings_instance.get_channel_settings.return_value = {}
        self.mock_channel_settings.return_value = channel_settings_instance
        
        # Setup regex patterns - only Channel 1 has patterns
        self.automation_manager.regex_matcher.add_channel_pattern(
            channel_id='1',
            name='Channel 1',
            regex_patterns=[r'News.*'],
            enabled=True
        )
        
        # Run validation
        results = self.automation_manager.validate_and_remove_non_matching_streams(force=True)
        
        # Assertions
        # Only 1 channel should be checked (Channel 1)
        self.assertEqual(results['channels_checked'], 1, 
                        "Only channels with regex patterns should be checked")
        
        # Stream 102 ('Sports Stream') should be removed from Channel 1 because it doesn't match 'News.*'
        self.assertEqual(results['streams_removed'], 1,
                        "Non-matching stream should be removed from Channel 1")
        
        # Channels 2 and 3 should not be affected
        # Verify update_channel_streams was only called once (for Channel 1)
        self.assertEqual(self.mock_update_streams.call_count, 1,
                        "update_channel_streams should only be called for Channel 1")
        
        # Verify the call was for Channel 1 with stream 101 (keeping only the matching stream)
        call_args = self.mock_update_streams.call_args
        self.assertEqual(call_args[0][0], 1, "Update should be for Channel 1")
        self.assertEqual(call_args[0][1], [101], "Only stream 101 should be kept")
    
    def test_channels_with_disabled_patterns_are_skipped(self):
        """Test that channels with disabled regex patterns are not validated."""
        
        # Setup: Create 1 channel with disabled pattern
        channels = [
            {'id': 1, 'name': 'Channel 1', 'channel_group_id': None}
        ]
        
        streams = [
            {'id': 101, 'name': 'Test Stream', 'm3u_account': 1}
        ]
        
        channel_streams = {
            1: [{'id': 101, 'name': 'Test Stream'}]
        }
        
        # Setup UDI mock
        udi_instance = MagicMock()
        udi_instance.get_channels.return_value = channels
        udi_instance.get_streams.return_value = streams
        udi_instance.get_channel_streams.side_effect = lambda ch_id: channel_streams.get(ch_id, [])
        self.mock_udi.return_value = udi_instance
        
        # Setup channel settings mock
        channel_settings_instance = MagicMock()
        channel_settings_instance._settings = {}
        channel_settings_instance.is_matching_enabled.return_value = True
        channel_settings_instance.is_channel_enabled_by_group.return_value = True
        channel_settings_instance.get_channel_settings.return_value = {}
        self.mock_channel_settings.return_value = channel_settings_instance
        
        # Setup regex patterns - Channel 1 has patterns but they're disabled
        self.automation_manager.regex_matcher.add_channel_pattern(
            channel_id='1',
            name='Channel 1',
            regex_patterns=[r'News.*'],
            enabled=False  # Pattern is disabled
        )
        
        # Run validation
        results = self.automation_manager.validate_and_remove_non_matching_streams(force=True)
        
        # Assertions
        # No channels should be checked because the pattern is disabled
        self.assertEqual(results['channels_checked'], 0,
                        "Channels with disabled patterns should be skipped")
        self.assertEqual(results['streams_removed'], 0,
                        "No streams should be removed")
        self.assertEqual(self.mock_update_streams.call_count, 0,
                        "update_channel_streams should not be called")
    
    def test_has_regex_patterns_method(self):
        """Test the has_regex_patterns helper method."""
        
        # Add patterns for channel 1
        self.regex_matcher.add_channel_pattern(
            channel_id='1',
            name='Channel 1',
            regex_patterns=[r'Test.*'],
            enabled=True
        )
        
        # Add disabled patterns for channel 2
        self.regex_matcher.add_channel_pattern(
            channel_id='2',
            name='Channel 2',
            regex_patterns=[r'Test.*'],
            enabled=False
        )
        
        # Channel 1 should have patterns
        self.assertTrue(self.regex_matcher.has_regex_patterns('1'),
                       "Channel 1 should have regex patterns")
        
        # Channel 2 should not have patterns (disabled)
        self.assertFalse(self.regex_matcher.has_regex_patterns('2'),
                        "Channel 2 should not have patterns (disabled)")
        
        # Channel 3 should not have patterns (doesn't exist)
        self.assertFalse(self.regex_matcher.has_regex_patterns('3'),
                        "Channel 3 should not have patterns (doesn't exist)")


if __name__ == '__main__':
    unittest.main()

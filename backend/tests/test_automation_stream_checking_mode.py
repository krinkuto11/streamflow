#!/usr/bin/env python3
"""
Unit test to verify that automation cycle respects stream checking mode.

This test verifies that when stream checking mode is active:
1. The automation cycle skips execution silently
2. No UDI refresh calls are made
3. No API requests are triggered
"""

import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.automation.automated_stream_manager import AutomatedStreamManager


class TestAutomationStreamCheckingMode(unittest.TestCase):
    """Test that automation cycle respects stream checking mode."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create temporary directory for test files
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / 'automation_config.json'
        self.changelog_file = Path(self.temp_dir) / 'changelog.json'
        self.regex_config_file = Path(self.temp_dir) / 'channel_regex_config.json'
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_automation_skips_when_stream_checking_mode_active(self):
        """Test that automation cycle skips when stream_checking_mode is True."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to return stream_checking_mode = True
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': True,
                'checking': False,
                'queue': {'queue_size': 1}  # One channel in queue
            }
            mock_stream_checker.get_status.return_value = mock_status
            
            # Mock the import and service getter
            with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                # Mock refresh_playlists to track if it's called
                manager.refresh_playlists = Mock()
                
                # Set last_playlist_update to None to ensure it would normally run
                manager.last_playlist_update = None
                
                # Run automation cycle
                manager.run_automation_cycle()
                
                # Verify that refresh_playlists was NOT called
                manager.refresh_playlists.assert_not_called()
                
                # Verify that get_status was called to check stream_checking_mode
                mock_stream_checker.get_status.assert_called_once()
    
    def test_automation_runs_when_stream_checking_mode_inactive(self):
        """Test that automation cycle runs when stream_checking_mode is False."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to return stream_checking_mode = False
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': False,
                'checking': False,
                'queue': {'queue_size': 0}
            }
            mock_stream_checker.get_status.return_value = mock_status
            
            # Mock the import and service getter
            with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                # Mock refresh_playlists to track if it's called
                manager.refresh_playlists = Mock(return_value=True)
                manager.discover_and_assign_streams = Mock(return_value={})
                
                # Set last_playlist_update to None to ensure it would run
                manager.last_playlist_update = None
                
                # Run automation cycle
                manager.run_automation_cycle()
                
                # Verify that refresh_playlists WAS called
                manager.refresh_playlists.assert_called_once()
                
                # Verify that get_status was called to check stream_checking_mode
                mock_stream_checker.get_status.assert_called_once()
    
    def test_automation_skips_silently_without_logging(self):
        """Test that automation cycle skips silently without debug logs when in stream checking mode."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service
            mock_stream_checker = Mock()
            mock_status = {
                'stream_checking_mode': True,
                'checking': False,
                'queue': {'queue_size': 0}
            }
            mock_stream_checker.get_status.return_value = mock_status
            
            # Mock logger to track logging calls
            with patch('automated_stream_manager.logger') as mock_logger:
                with patch('stream_checker_service.get_stream_checker_service', return_value=mock_stream_checker):
                    manager.refresh_playlists = Mock()
                    manager.last_playlist_update = None
                    
                    # Run automation cycle
                    manager.run_automation_cycle()
                    
                    # Verify no INFO or DEBUG logs about "Running" or "completed"
                    # Only debug log should be if there's an error checking status
                    for call_args in mock_logger.debug.call_args_list:
                        args = call_args[0]
                        if args:
                            message = args[0]
                            # Ensure we're not logging "Running automation cycle..." or similar
                            self.assertNotIn("Running automation cycle", message)
                            self.assertNotIn("completed", message.lower())
                    
                    for call_args in mock_logger.info.call_args_list:
                        args = call_args[0]
                        if args:
                            message = args[0]
                            # Ensure we're not logging about starting or completing cycle
                            self.assertNotIn("Starting automation cycle", message)
                            self.assertNotIn("Automation cycle completed", message)
    
    def test_status_check_handles_exception_gracefully(self):
        """Test that automation continues if stream checker status check fails."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager(config_file=self.config_file)
            
            # Mock stream checker service to raise an exception
            def raise_exception():
                raise Exception("Stream checker not available")
            
            with patch('stream_checker_service.get_stream_checker_service', side_effect=raise_exception):
                # Mock refresh methods
                manager.refresh_playlists = Mock(return_value=True)
                manager.discover_and_assign_streams = Mock(return_value={})
                manager.last_playlist_update = None
                
                # Run automation cycle - should handle exception and continue
                manager.run_automation_cycle()
                
                # Verify that refresh_playlists WAS called (exception was handled)
                manager.refresh_playlists.assert_called_once()


if __name__ == '__main__':
    # Run tests with verbose output
    unittest.main(verbosity=2)

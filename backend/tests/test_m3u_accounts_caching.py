#!/usr/bin/env python3
"""
Unit tests for M3U accounts caching optimization.

This module tests that M3U accounts are only queried once per playlist refresh cycle
to reduce API calls to the Universal Data Index.

The optimization ensures:
- refresh_playlists() caches the M3U accounts
- discover_and_assign_streams() uses the cached accounts instead of fetching again
- Cache is cleared after each automation cycle
"""

import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.automation.automated_stream_manager import AutomatedStreamManager


class TestM3UAccountsCaching(unittest.TestCase):
    """Test M3U accounts caching optimization."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_file = Path(self.temp_dir) / 'automation_config.json'
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_m3u_accounts_cache_initialized_to_none(self):
        """Test that M3U accounts cache is initialized to None."""
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            self.assertIsNone(manager._m3u_accounts_cache)
    
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_refresh_playlists_caches_m3u_accounts(self, mock_get_accounts, mock_get_streams, mock_refresh):
        """Test that refresh_playlists caches M3U accounts."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True},
            {'id': 2, 'name': 'Account 2', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = []
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # Initially cache should be None
            self.assertIsNone(manager._m3u_accounts_cache)
            
            # After refresh_playlists, cache should contain accounts
            manager.refresh_playlists()
            self.assertEqual(manager._m3u_accounts_cache, test_accounts)
    
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_discover_uses_cached_m3u_accounts(self, mock_get_accounts, mock_get_streams, 
                                                mock_refresh, mock_udi, mock_add_streams):
        """Test that discover_and_assign_streams uses cached M3U accounts."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True},
            {'id': 2, 'name': 'Account 2', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # First call refresh_playlists which should cache accounts
            manager.refresh_playlists()
            
            # Reset mock call count to test discover_and_assign_streams
            initial_call_count = mock_get_accounts.call_count
            
            # Call discover_and_assign_streams - should use cached accounts
            manager.discover_and_assign_streams()
            
            # get_m3u_accounts should NOT be called again (uses cache)
            self.assertEqual(mock_get_accounts.call_count, initial_call_count)
    
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_discover_fetches_when_cache_empty(self, mock_get_accounts, mock_get_streams,
                                                mock_udi, mock_add_streams):
        """Test that discover_and_assign_streams fetches when cache is empty."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # Cache should be None initially
            self.assertIsNone(manager._m3u_accounts_cache)
            
            # Call discover_and_assign_streams without refresh_playlists first
            manager.discover_and_assign_streams()
            
            # get_m3u_accounts should be called since cache was empty
            mock_get_accounts.assert_called()
    
    @patch('stream_checker_service.get_stream_checker_service')
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_automation_cycle_clears_cache(self, mock_get_accounts, mock_get_streams,
                                           mock_refresh, mock_udi, mock_add_streams,
                                           mock_stream_checker):
        """Test that run_automation_cycle clears cache after completion."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = []
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        # Mock stream checker to avoid import issues
        mock_sc = MagicMock()
        mock_sc.get_status.return_value = {'stream_checking_mode': False}
        mock_stream_checker.return_value = mock_sc
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.time.sleep'):
                manager = AutomatedStreamManager()
                manager.config['enabled_features']['changelog_tracking'] = False
                manager.last_playlist_update = None  # Force update
                
                # Run automation cycle
                manager.run_automation_cycle()
                
                # Cache should be cleared after cycle completes
                self.assertIsNone(manager._m3u_accounts_cache)
    
    @patch('stream_checker_service.get_stream_checker_service')
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_only_one_m3u_accounts_call_per_cycle(self, mock_get_accounts, mock_get_streams,
                                                   mock_refresh, mock_udi, mock_add_streams,
                                                   mock_stream_checker):
        """Test that M3U accounts are only fetched once during a full automation cycle."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True},
            {'id': 2, 'name': 'Account 2', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        # Mock stream checker
        mock_sc = MagicMock()
        mock_sc.get_status.return_value = {'stream_checking_mode': False}
        mock_stream_checker.return_value = mock_sc
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            with patch('automated_stream_manager.time.sleep'):
                manager = AutomatedStreamManager()
                manager.config['enabled_features']['changelog_tracking'] = False
                manager.last_playlist_update = None  # Force update
                
                # Run automation cycle (calls refresh_playlists + discover_and_assign_streams)
                manager.run_automation_cycle()
                
                # get_m3u_accounts should only be called ONCE during the entire cycle
                # (in refresh_playlists, cached for discover_and_assign_streams)
                self.assertEqual(mock_get_accounts.call_count, 1,
                    f"Expected 1 call to get_m3u_accounts, got {mock_get_accounts.call_count}")


class TestM3UAccountsCachingEdgeCases(unittest.TestCase):
    """Test edge cases for M3U accounts caching."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_cache_persists_after_refresh_error(self, mock_get_accounts, mock_get_streams, mock_refresh):
        """Test that cache behavior is correct even when refresh fails."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = []
        mock_refresh.side_effect = Exception("Refresh failed")
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # refresh_playlists should fail but cache should have been set before the error
            result = manager.refresh_playlists()
            
            # The function catches the exception and returns False
            self.assertFalse(result)
            
            # Cache should have been set before the exception occurred
            # (accounts are fetched before refresh is called)
            self.assertEqual(manager._m3u_accounts_cache, test_accounts)
    
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_cache_none_when_get_accounts_returns_none(self, mock_get_accounts, mock_get_streams, mock_udi):
        """Test behavior when get_m3u_accounts returns None."""
        mock_get_accounts.return_value = None
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'is_custom': True}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # discover_and_assign_streams should handle None cache correctly
            manager._m3u_accounts_cache = None
            result = manager.discover_and_assign_streams()
            
            # Should have called get_m3u_accounts since cache was None
            mock_get_accounts.assert_called()
            
            # Result should be empty dict (no channels)
            self.assertEqual(result, {})


class TestM3UAccountsCachingDebugLogs(unittest.TestCase):
    """Test debug logging for M3U accounts caching."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        import logging
        from io import StringIO
        import apps.automation.automated_stream_manager
        
        # Set up logging capture
        self.log_stream = StringIO()
        self.handler = logging.StreamHandler(self.log_stream)
        self.handler.setLevel(logging.DEBUG)
        
        # Get the logger used by automated_stream_manager dynamically from the module's __name__
        # This ensures we're capturing logs from the correct logger
        self.logger_name = automated_stream_manager.__name__
        self.logger = logging.getLogger(self.logger_name)
        self.original_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        
        # Restore logging
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.original_level)
    
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.refresh_m3u_playlists')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_debug_log_when_using_cached_accounts(self, mock_get_accounts, mock_get_streams, 
                                                    mock_refresh, mock_udi, mock_add_streams):
        """Test that debug log correctly indicates when cached M3U accounts are used."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True},
            {'id': 2, 'name': 'Account 2', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # First call refresh_playlists which should cache accounts
            manager.refresh_playlists()
            
            # Then call discover_and_assign_streams which should use cached accounts
            manager.discover_and_assign_streams()
            
            # Check log output
            log_output = self.log_stream.getvalue()
            
            # Should have logged that cached accounts are being used
            self.assertIn("Using cached M3U accounts", log_output)
            self.assertIn("no UDI/API call", log_output)
    
    @patch('automated_stream_manager.add_streams_to_channel')
    @patch('automated_stream_manager.get_udi_manager')
    @patch('automated_stream_manager.get_streams')
    @patch('automated_stream_manager.get_m3u_accounts')
    def test_debug_log_when_fetching_accounts(self, mock_get_accounts, mock_get_streams,
                                               mock_udi, mock_add_streams):
        """Test that debug log correctly indicates when M3U accounts are fetched from UDI."""
        test_accounts = [
            {'id': 1, 'name': 'Account 1', 'is_active': True}
        ]
        mock_get_accounts.return_value = test_accounts
        mock_get_streams.return_value = [
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1}
        ]
        
        # Mock UDI manager
        mock_udi_instance = MagicMock()
        mock_udi_instance.get_channels.return_value = []
        mock_udi.return_value = mock_udi_instance
        
        with patch('automated_stream_manager.CONFIG_DIR', Path(self.temp_dir)):
            manager = AutomatedStreamManager()
            manager.config['enabled_features']['changelog_tracking'] = False
            
            # Call discover_and_assign_streams without refresh_playlists first (cache empty)
            manager.discover_and_assign_streams()
            
            # Check log output
            log_output = self.log_stream.getvalue()
            
            # Should have logged that accounts were fetched from UDI cache
            self.assertIn("Fetched M3U accounts from UDI cache", log_output)
            self.assertIn("cache was empty", log_output)


if __name__ == '__main__':
    unittest.main()

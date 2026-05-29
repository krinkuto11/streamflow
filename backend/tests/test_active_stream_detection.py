#!/usr/bin/env python3
"""
Tests for active stream detection using proxy status correlation.

This test suite verifies that the UDI Manager correctly correlates proxy status
with M3U account profiles to detect active streams.
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.manager import UDIManager


class TestActiveStreamDetection(unittest.TestCase):
    """Test active stream detection using m3u_profile_id correlation."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.manager = UDIManager()
        self.manager._initialized = True
        
        # Mock M3U accounts with profiles
        self.manager._m3u_accounts_cache = [
            {
                'id': 1,
                'name': 'Account 1',
                'profiles': [
                    {'id': 5, 'name': 'Profile 5'},
                    {'id': 6, 'name': 'Profile 6'}
                ]
            },
            {
                'id': 2,
                'name': 'Account 2',
                'profiles': [
                    {'id': 7, 'name': 'Profile 7'}
                ]
            }
        ]
        self.manager._build_indexes()
    
    def test_count_active_streams_with_profile_correlation(self):
        """Test counting active streams using m3u_profile_id from proxy status."""
        # Mock proxy status with channels using different profiles
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                'm3u_profile_id': 5,  # Belongs to account 1
                'client_count': 1
            },
            'channel-uuid-2': {
                'channel_id': 'channel-uuid-2',
                'state': 'active',
                'm3u_profile_id': 6,  # Also belongs to account 1
                'client_count': 1
            },
            'channel-uuid-3': {
                'channel_id': 'channel-uuid-3',
                'state': 'active',
                'm3u_profile_id': 7,  # Belongs to account 2
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            # Account 1 should have 2 active streams (profiles 5 and 6)
            count = self.manager._count_active_streams(1)
            self.assertEqual(count, 2)
            
            # Account 2 should have 1 active stream (profile 7)
            count = self.manager._count_active_streams(2)
            self.assertEqual(count, 1)
            
            # Account 3 (non-existent) should have 0 active streams
            count = self.manager._count_active_streams(3)
            self.assertEqual(count, 0)
    
    def test_count_active_streams_only_counts_active_state(self):
        """Test that only channels with active state are counted."""
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                'm3u_profile_id': 5,
                'client_count': 1
            },
            'channel-uuid-2': {
                'channel_id': 'channel-uuid-2',
                'state': 'idle',  # Not active
                'm3u_profile_id': 6,
                'client_count': 0
            },
            'channel-uuid-3': {
                'channel_id': 'channel-uuid-3',
                'state': 'buffering',  # Not active
                'm3u_profile_id': 5,
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            # Only channel-uuid-1 is active, so count should be 1
            count = self.manager._count_active_streams(1)
            self.assertEqual(count, 1)
    
    def test_count_active_streams_handles_missing_profile_id(self):
        """Test handling of proxy status without m3u_profile_id."""
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                # Missing m3u_profile_id
                'client_count': 1
            },
            'channel-uuid-2': {
                'channel_id': 'channel-uuid-2',
                'state': 'active',
                'm3u_profile_id': 5,
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            # Should only count channel-uuid-2 which has profile_id
            count = self.manager._count_active_streams(1)
            self.assertEqual(count, 1)
    
    def test_count_active_streams_handles_unknown_profile(self):
        """Test handling of proxy status with unknown profile ID."""
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                'm3u_profile_id': 999,  # Unknown profile
                'client_count': 1
            },
            'channel-uuid-2': {
                'channel_id': 'channel-uuid-2',
                'state': 'active',
                'm3u_profile_id': 5,  # Known profile
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            # Should only count channel-uuid-2
            count = self.manager._count_active_streams(1)
            self.assertEqual(count, 1)
    
    def test_count_active_streams_empty_proxy_status(self):
        """Test counting with empty proxy status."""
        with patch.object(self.manager, '_get_proxy_status', return_value={}):
            count = self.manager._count_active_streams(1)
            self.assertEqual(count, 0)
    
    def test_get_active_streams_for_account(self):
        """Test the public method get_active_streams_for_account."""
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                'm3u_profile_id': 5,
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            count = self.manager.get_active_streams_for_account(1)
            self.assertEqual(count, 1)
    
    def test_get_active_streams_for_profile(self):
        """Test getting active streams for a specific profile."""
        mock_proxy_status = {
            'channel-uuid-1': {
                'channel_id': 'channel-uuid-1',
                'state': 'active',
                'm3u_profile_id': 5,
                'client_count': 1
            }
        }
        
        with patch.object(self.manager, '_get_proxy_status', return_value=mock_proxy_status):
            # Profile 5 belongs to account 1
            count = self.manager.get_active_streams_for_profile(5)
            self.assertEqual(count, 1)
    
    def test_find_account_for_profile(self):
        """Test finding the account for a given profile."""
        # Profile 5 should belong to account 1
        account_id = self.manager._find_account_for_profile(5)
        self.assertEqual(account_id, 1)
        
        # Profile 6 should also belong to account 1
        account_id = self.manager._find_account_for_profile(6)
        self.assertEqual(account_id, 1)
        
        # Profile 7 should belong to account 2
        account_id = self.manager._find_account_for_profile(7)
        self.assertEqual(account_id, 2)
        
        # Unknown profile should return None
        account_id = self.manager._find_account_for_profile(999)
        self.assertIsNone(account_id)
    
    def test_is_channel_status_active(self):
        """Test the _is_channel_status_active method."""
        # Test with 'state' field (current format)
        status = {'state': 'active'}
        self.assertTrue(self.manager._is_channel_status_active(status))
        
        status = {'state': 'idle'}
        self.assertFalse(self.manager._is_channel_status_active(status))
        
        # Test with clients
        status = {'clients': [{'id': 'client1'}]}
        self.assertTrue(self.manager._is_channel_status_active(status))
        
        status = {'clients': []}
        self.assertFalse(self.manager._is_channel_status_active(status))
        
        # Test with no indicators
        status = {}
        self.assertFalse(self.manager._is_channel_status_active(status))
        
        # Test with invalid input
        self.assertFalse(self.manager._is_channel_status_active(None))
        self.assertFalse(self.manager._is_channel_status_active("string"))


if __name__ == '__main__':
    unittest.main()

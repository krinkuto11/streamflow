#!/usr/bin/env python3
"""
Tests for proxy status integration in UDI for accurate active stream counting.

This test suite verifies that:
1. UDI can fetch proxy status from the Dispatcharr API
2. Active stream counting uses real-time proxy status
3. The proxy status cache works correctly with TTL
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch, MagicMock
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.manager import UDIManager


class TestProxyStatusIntegration(unittest.TestCase):
    """Test proxy status integration for active stream counting."""
    
    def setUp(self):
        """Set up test fixtures."""
        # Create UDI Manager
        self.udi = UDIManager()
        self.udi._initialized = True
        
        # Test M3U accounts
        self.udi._m3u_accounts_cache = [
            {
                'id': 1,
                'name': 'Test Account 1',
                'max_streams': 2,
                'profiles': [
                    {'id': 101, 'name': 'Profile 1', 'max_streams': 2}
                ]
            },
            {
                'id': 2,
                'name': 'Test Account 2',
                'max_streams': 5,
                'profiles': [
                    {'id': 201, 'name': 'Profile 2', 'max_streams': 5}
                ]
            }
        ]
        
        # Test channels
        self.udi._channels_cache = [
            {'id': 100, 'uuid': 'channel-100-uuid', 'name': 'Channel 100', 'streams': [1, 2]},
            {'id': 101, 'name': 'Channel 101', 'streams': [3]},
            {'id': 102, 'name': 'Channel 102', 'streams': [4]},
            {'id': 103, 'name': 'Channel 103', 'streams': [5, 6]},
        ]
        
        # Build channel index
        self.udi._channels_by_id = {ch['id']: ch for ch in self.udi._channels_cache}
        
        # Test streams
        self.udi._streams_cache = [
            # Account 1 streams
            {'id': 1, 'name': 'Stream 1', 'm3u_account': 1, 'current_viewers': 0},
            {'id': 2, 'name': 'Stream 2', 'm3u_account': 1, 'current_viewers': 0},
            {'id': 3, 'name': 'Stream 3', 'm3u_account': 1, 'current_viewers': 0},
            # Account 2 streams
            {'id': 4, 'name': 'Stream 4', 'm3u_account': 2, 'current_viewers': 0},
            {'id': 5, 'name': 'Stream 5', 'm3u_account': 2, 'current_viewers': 0},
            {'id': 6, 'name': 'Stream 6', 'm3u_account': 2, 'current_viewers': 0},
        ]
        
        # Build stream index
        self.udi._streams_by_id = {s['id']: s for s in self.udi._streams_cache}
        self.udi._build_indexes()
    
    def test_count_active_streams_with_proxy_status(self):
        """Test counting active streams using proxy status."""
        # Mock proxy status showing channels 100 and 102 are active
        mock_proxy_status = {
            '100': {
                'channel_id': 100,
                'current_stream': 'http://example.com/stream1',
                'active': True,
                'm3u_profile_id': 101,
                'clients': [{'id': 'client1'}]
            },
            '102': {
                'channel_id': 102,
                'current_stream': 'http://example.com/stream4',
                'active': True,
                'm3u_profile_id': 201,
                'clients': [{'id': 'client2'}]
            }
        }
        
        # Mock the fetcher to return our test proxy status
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            # Account 1 has channels 100 and 101, but only 100 is active
            active_count_acc1 = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count_acc1, 1, "Account 1 should have 1 active stream (channel 100)")
            
            # Account 2 has channels 102 and 103, but only 102 is active
            active_count_acc2 = self.udi.get_active_streams_for_account(2)
            self.assertEqual(active_count_acc2, 1, "Account 2 should have 1 active stream (channel 102)")
    
    def test_count_active_streams_no_active_channels(self):
        """Test counting when no channels are active."""
        # Mock proxy status showing no active channels
        mock_proxy_status = {}
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            active_count_acc1 = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count_acc1, 0, "Account 1 should have 0 active streams")
            
            active_count_acc2 = self.udi.get_active_streams_for_account(2)
            self.assertEqual(active_count_acc2, 0, "Account 2 should have 0 active streams")
    
    def test_count_active_streams_all_channels_active(self):
        """Test counting when all channels are active."""
        # Mock proxy status showing all channels active
        mock_proxy_status = {
            '100': {'channel_id': 100, 'active': True, 'm3u_profile_id': 101},
            '101': {'channel_id': 101, 'active': True, 'm3u_profile_id': 101},
            '102': {'channel_id': 102, 'active': True, 'm3u_profile_id': 201},
            '103': {'channel_id': 103, 'active': True, 'm3u_profile_id': 201},
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            # Account 1 has 2 channels (100, 101)
            active_count_acc1 = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count_acc1, 2, "Account 1 should have 2 active streams")
            
            # Account 2 has 2 channels (102, 103)
            active_count_acc2 = self.udi.get_active_streams_for_account(2)
            self.assertEqual(active_count_acc2, 2, "Account 2 should have 2 active streams")
    
    def test_proxy_status_cache_ttl(self):
        """Test that proxy status cache respects TTL."""
        mock_proxy_status_1 = {'100': {'active': True}}
        mock_proxy_status_2 = {'100': {'active': True}, '101': {'active': True}}
        
        # Mock the fetcher
        mock_fetcher = MagicMock()
        self.udi.fetcher.fetch_proxy_status = mock_fetcher
        
        # First call should fetch
        mock_fetcher.return_value = mock_proxy_status_1
        status1 = self.udi._get_proxy_status()
        self.assertEqual(len(status1), 1)
        self.assertEqual(mock_fetcher.call_count, 1)
        
        # Second call within TTL should use cache
        status2 = self.udi._get_proxy_status()
        self.assertEqual(len(status2), 1)
        self.assertEqual(mock_fetcher.call_count, 1)  # Still 1, no new call
        
        # Wait for cache to expire (6 seconds > 5 second TTL)
        self.udi._proxy_status_last_fetch = time.time() - 6
        
        # Third call should fetch fresh data
        mock_fetcher.return_value = mock_proxy_status_2
        status3 = self.udi._get_proxy_status()
        self.assertEqual(len(status3), 2)
        self.assertEqual(mock_fetcher.call_count, 2)  # Now 2, new call made
    
    def test_proxy_status_force_refresh(self):
        """Test forcing a proxy status refresh."""
        mock_proxy_status_1 = {'100': {'active': True}}
        mock_proxy_status_2 = {'100': {'active': True}, '101': {'active': True}}
        
        # Mock the fetcher
        mock_fetcher = MagicMock()
        self.udi.fetcher.fetch_proxy_status = mock_fetcher
        
        # First call
        mock_fetcher.return_value = mock_proxy_status_1
        status1 = self.udi._get_proxy_status()
        self.assertEqual(len(status1), 1)
        self.assertEqual(mock_fetcher.call_count, 1)
        
        # Force refresh should fetch even within TTL
        mock_fetcher.return_value = mock_proxy_status_2
        status2 = self.udi._get_proxy_status(force_refresh=True)
        self.assertEqual(len(status2), 2)
        self.assertEqual(mock_fetcher.call_count, 2)
    
    def test_proxy_status_error_handling(self):
        """Test handling of proxy status fetch errors."""
        # Mock the fetcher to raise an exception
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', side_effect=Exception("Network error")):
            # Should return empty dict on error
            status = self.udi._get_proxy_status()
            self.assertEqual(status, {})
            
            # Active count should be 0 when proxy status unavailable
            active_count = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count, 0)
    
    def test_proxy_status_with_clients_field(self):
        """Test proxy status detection using clients field."""
        # Mock proxy status using clients field
        mock_proxy_status = {
            '100': {
                'channel_id': 100,
                'm3u_profile_id': 101,
                'clients': [
                    {'id': 'client1', 'connected': True},
                    {'id': 'client2', 'connected': True}
                ]
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            active_count = self.udi.get_active_streams_for_account(1)
            self.assertEqual(active_count, 1, "Should detect active stream from clients field")
    
    def test_is_channel_active(self):
        """Test checking if a specific channel is active."""
        # Mock proxy status with channel 100 active
        mock_proxy_status = {
            '100': {
                'channel_id': 100,
                'current_stream': 'http://example.com/stream1',
                'active': True
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            # Channel 100 should be active
            is_active = self.udi.is_channel_active(100)
            self.assertTrue(is_active, "Channel 100 should be active")
            
            # Channel 101 should not be active
            is_active = self.udi.is_channel_active(101)
            self.assertFalse(is_active, "Channel 101 should not be active")
    
    def test_is_channel_active_with_clients(self):
        """Test checking if a channel is active based on clients field."""
        # Mock proxy status with channel 100 having active clients
        mock_proxy_status = {
            '100': {
                'channel_id': 100,
                'clients': [{'id': 'client1'}]
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            is_active = self.udi.is_channel_active(100)
            self.assertTrue(is_active, "Channel 100 should be active due to clients")
    
    def test_is_channel_active_empty_clients(self):
        """Test that empty clients list means channel is not active."""
        # Mock proxy status with channel 100 having empty clients
        mock_proxy_status = {
            '100': {
                'channel_id': 100,
                'clients': []
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            is_active = self.udi.is_channel_active(100)
            self.assertFalse(is_active, "Channel 100 should not be active with empty clients")
    
    def test_is_channel_active_with_state_field(self):
        """Test checking if a channel is active based on state field (new API format)."""
        # Mock proxy status with channel 100 having state='active'
        mock_proxy_status = {
            '100': {
                'channel_id': '100',
                'state': 'active',
                'url': 'http://example.com/stream',
                'stream_profile': '1',
                'owner': '8ab76bb6f5f3:174',
                'buffer_index': 1446,
                'client_count': 1,
                'uptime': 693.8248314857483,
                'stream_id': 11554,
                'stream_name': 'Test Stream',
                'total_bytes': 365813760,
                'avg_bitrate_kbps': 4217.9379393689405,
                'clients': [
                    {
                        'client_id': 'client_1767279803960_3331',
                        'user_agent': 'VLC/3.0.21 LibVLC/3.0.21',
                        'ip_address': '79.116.168.102',
                        'access_type': 'M3U',
                        'connected_since': 693.7255585193634
                    }
                ]
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            is_active = self.udi.is_channel_active(100)
            self.assertTrue(is_active, "Channel 100 should be active due to state='active'")
    
    def test_is_channel_not_active_with_state_field(self):
        """Test that channel is not active when state is not 'active'."""
        # Mock proxy status with channel 100 having state='idle' or other value
        mock_proxy_status = {
            '100': {
                'channel_id': '100',
                'state': 'idle'
            }
        }
        
        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            is_active = self.udi.is_channel_active(100)
            self.assertFalse(is_active, "Channel 100 should not be active when state is not 'active'")

    def test_is_channel_active_with_uuid_keyed_proxy_status(self):
        """Test UUID-keyed Dispatcharr proxy status still protects numeric channel IDs."""
        mock_proxy_status = {
            'channel-100-uuid': {
                'channel_id': 'channel-100-uuid',
                'state': 'active',
                'clients': [{'id': 'client1'}],
            }
        }

        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            self.assertTrue(
                self.udi.is_channel_active(100),
                "Channel 100 should be active when proxy status is keyed by channel UUID",
            )
            self.assertFalse(
                self.udi.is_channel_active(101),
                "Channel 101 should not be active when UUID belongs to channel 100",
            )

    def test_is_channel_active_with_uuid_value_under_unrelated_key(self):
        """Test matching by status payload UUID when the proxy key is not numeric."""
        mock_proxy_status = {
            'proxy-entry-1': {
                'channel_uuid': 'channel-100-uuid',
                'state': 'active',
                'client_count': 1,
            }
        }

        with patch.object(self.udi.fetcher, 'fetch_proxy_status', return_value=mock_proxy_status):
            self.assertTrue(
                self.udi.is_channel_active(100),
                "Channel 100 should be active when proxy payload contains its UUID",
            )


if __name__ == '__main__':
    unittest.main()

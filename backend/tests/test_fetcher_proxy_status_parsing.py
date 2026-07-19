#!/usr/bin/env python3
"""
Tests for proxy status response parsing in UDIFetcher.

This test suite verifies that the fetcher correctly parses the standard
format of the /proxy/ts/status endpoint response.
"""

import sys
import os
import unittest
from unittest.mock import Mock, patch

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.fetcher import (
    UDIFetcher,
    ProxyStatusConfigurationError,
    ProxyStatusPayloadError,
    ProxyStatusTransportError,
)


class TestFetcherProxyStatusParsing(unittest.TestCase):
    """Test proxy status response parsing."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.fetcher = UDIFetcher()
        # Ensure base_url is set for tests
        self.fetcher.base_url = "http://test-dispatcharr.local"
    
    def test_parse_standard_format_with_channels_array(self):
        """Test parsing the standard API format with nested channels array."""
        # This is the actual format returned by /proxy/ts/status
        mock_response = {
            "channels": [
                {
                    "channel_id": "c4fa030c-a0b9-4df1-83fe-4680ed8f3c89",
                    "state": "active",
                    "url": "http://acexy:8080/ace/getstream?id=00c9bc9c5d7d87680a5a6bed349edfa775a89947",
                    "stream_profile": "1",
                    "owner": "8ab76bb6f5f3:174",
                    "buffer_index": 1446,
                    "client_count": 1,
                    "uptime": 693.8248314857483,
                    "stream_id": 11554,
                    "stream_name": "M+ LALIGA --> ELCANO",
                    "total_bytes": 365813760,
                    "avg_bitrate_kbps": 4217.9379393689405,
                    "avg_bitrate": "4.22 Mbps",
                    "clients": [
                        {
                            "client_id": "client_1767279803960_3331",
                            "user_agent": "VLC/3.0.21 LibVLC/3.0.21",
                            "ip_address": "79.116.168.102",
                            "access_type": "M3U",
                            "connected_since": 693.7255585193634
                        }
                    ],
                    "m3u_profile_id": 6,
                    "m3u_profile_name": "IPFS NewERA Default",
                    "video_codec": "h264",
                    "resolution": "1920x1080",
                    "source_fps": 25.0,
                    "ffmpeg_speed": 1.11,
                    "audio_codec": "ac3",
                    "audio_channels": "stereo",
                    "stream_type": "mpegts"
                }
            ],
            "count": 1
        }
        
        # Mock the _fetch_url method to return our test data
        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            result = self.fetcher.fetch_proxy_status()
            
            # Should convert to dict keyed by channel_id
            self.assertIsInstance(result, dict)
            self.assertEqual(len(result), 1)
            
            # Channel ID should be the key
            channel_id = "c4fa030c-a0b9-4df1-83fe-4680ed8f3c89"
            self.assertIn(channel_id, result)
            
            # Verify all fields are preserved
            channel_data = result[channel_id]
            self.assertEqual(channel_data['state'], 'active')
            self.assertEqual(channel_data['stream_name'], 'M+ LALIGA --> ELCANO')
            self.assertEqual(channel_data['client_count'], 1)
            self.assertEqual(channel_data['m3u_profile_id'], 6)
            self.assertIsInstance(channel_data['clients'], list)
            self.assertEqual(len(channel_data['clients']), 1)
    
    def test_parse_standard_format_multiple_channels(self):
        """Test parsing standard format with multiple channels."""
        mock_response = {
            "channels": [
                {
                    "channel_id": "uuid-100",
                    "state": "active",
                    "stream_name": "Channel 100",
                    "m3u_profile_id": 5
                },
                {
                    "channel_id": "uuid-200",
                    "state": "active",
                    "stream_name": "Channel 200",
                    "m3u_profile_id": 6
                },
                {
                    "channel_id": "uuid-300",
                    "state": "idle",
                    "stream_name": "Channel 300",
                    "m3u_profile_id": 5
                }
            ],
            "count": 3
        }
        
        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            result = self.fetcher.fetch_proxy_status()
            
            self.assertIsInstance(result, dict)
            self.assertEqual(len(result), 3)
            self.assertIn("uuid-100", result)
            self.assertIn("uuid-200", result)
            self.assertIn("uuid-300", result)
            
            # Verify state and profile are preserved
            self.assertEqual(result["uuid-100"]["state"], "active")
            self.assertEqual(result["uuid-100"]["m3u_profile_id"], 5)
            self.assertEqual(result["uuid-200"]["state"], "active")
            self.assertEqual(result["uuid-200"]["m3u_profile_id"], 6)
            self.assertEqual(result["uuid-300"]["state"], "idle")
            self.assertEqual(result["uuid-300"]["m3u_profile_id"], 5)
    
    def test_parse_empty_channels_array(self):
        """Test parsing when channels array is empty."""
        mock_response = {
            "channels": [],
            "count": 0
        }
        
        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            result = self.fetcher.fetch_proxy_status()
            
            self.assertIsInstance(result, dict)
            self.assertEqual(len(result), 0)
    
    def test_parse_missing_channel_id_is_not_authoritative(self):
        """A partial channel list must never be mistaken for lower usage."""
        mock_response = {
            "channels": [
                {
                    "channel_id": "uuid-100",
                    "state": "active"
                },
                {
                    # Missing channel_id
                    "state": "active"
                },
                {
                    "channel_id": "uuid-200",
                    "state": "active"
                }
            ],
            "count": 3
        }
        
        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            with self.assertRaises(ProxyStatusPayloadError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_channel_id_invalid")
    
    def test_parse_invalid_response(self):
        """Test handling of invalid response format."""
        mock_response = "invalid response"
        
        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            with self.assertRaises(ProxyStatusPayloadError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_payload_not_mapping")
    
    def test_parse_error_handling(self):
        """Test error handling during fetch."""
        with patch.object(self.fetcher, '_fetch_url', side_effect=Exception("Network error")):
            with self.assertRaises(ProxyStatusTransportError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_transport_failed")

    def test_none_transport_result_is_not_authoritative_idle(self):
        """The generic request helper's failure sentinel must remain unknown."""
        with patch.object(self.fetcher, '_fetch_url', return_value=None):
            with self.assertRaises(ProxyStatusTransportError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_transport_failed")

    def test_count_mismatch_is_not_authoritative(self):
        """A truncated channel list must not reduce provider usage."""
        mock_response = {
            "channels": [{"channel_id": "uuid-100", "state": "active"}],
            "count": 2,
        }

        with patch.object(self.fetcher, '_fetch_url', return_value=mock_response):
            with self.assertRaises(ProxyStatusPayloadError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_count_mismatch")

    def test_missing_count_is_not_authoritative(self):
        """Only the complete status envelope can prove provider idleness."""
        with patch.object(
            self.fetcher,
            '_fetch_url',
            return_value={"channels": []},
        ):
            with self.assertRaises(ProxyStatusPayloadError) as raised:
                self.fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_count_invalid")
    
    def test_no_base_url(self):
        """Test handling when base_url is not set."""
        fetcher = UDIFetcher()
        fetcher.base_url = None
        
        with self.assertRaises(ProxyStatusConfigurationError) as raised:
            fetcher.fetch_proxy_status()

        self.assertEqual(raised.exception.reason, "proxy_status_base_url_missing")


if __name__ == '__main__':
    unittest.main()

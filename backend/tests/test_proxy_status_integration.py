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
import threading
import unittest
import pytest
from unittest.mock import Mock, patch, MagicMock
import time

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.manager import UDIManager
from apps.udi.fetcher import (
    ProxyStatusConfigurationError,
    ProxyStatusPayloadError,
    ProxyStatusTransportError,
)

pytestmark = pytest.mark.integration


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

    def _run_concurrent_strict_usage_calls(self, raw_fetch_result):
        """Run ten capacity reads while deterministically holding one fetch."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        request_lock = threading.Lock()
        request_count = [0]
        results = []
        errors = []
        output_lock = threading.Lock()
        barrier = threading.Barrier(11)

        def fetch_once(_url):
            with request_lock:
                request_count[0] += 1
            fetch_started.set()
            if not release_fetch.wait(timeout=2):
                raise AssertionError("test proxy fetch release timed out")
            return raw_fetch_result

        self.udi.fetcher._fetch_url = fetch_once

        def read_usage():
            try:
                barrier.wait(timeout=2)
                result = self.udi.get_active_stream_context_per_profile(1)
                with output_lock:
                    results.append(result)
            except BaseException as exc:  # surfaced in the parent test thread
                with output_lock:
                    errors.append(exc)

        workers = [threading.Thread(target=read_usage) for _ in range(10)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)

        fetch_observed = fetch_started.wait(timeout=2)
        with self.udi._proxy_status_condition:
            all_waiters_observed = self.udi._proxy_status_condition.wait_for(
                lambda: self.udi._proxy_status_waiter_count == 9,
                timeout=2,
            )
        release_fetch.set()
        for worker in workers:
            worker.join(timeout=2)

        self.assertTrue(fetch_observed)
        self.assertTrue(all_waiters_observed)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(request_count[0], 1)
        return results, errors
    
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
            # Non-capacity callers retain the historical fail-soft shape.
            status = self.udi._get_proxy_status()
            self.assertEqual(status, {})

            # Capacity callers must never interpret the same failure as idle.
            with self.assertRaises(ProxyStatusTransportError) as raised:
                self.udi.get_active_streams_for_account(1)
            self.assertEqual(raised.exception.reason, "proxy_status_fetch_failed")

    def test_valid_idle_status_is_cached_as_authoritative(self):
        """A confirmed empty status is healthy and cacheable."""
        fetch = Mock(return_value={})
        self.udi.fetcher.fetch_proxy_status = fetch

        self.assertEqual(self.udi._get_proxy_status(), {})
        self.assertEqual(self.udi._get_proxy_status(), {})
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(self.udi._proxy_status_cache_authoritative)
        self.assertIsNone(self.udi._proxy_status_last_error)

    def test_strict_usage_forces_refresh_past_fresh_idle_cache(self):
        """A newly started viewer cannot hide behind the one-second UI cache."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        idle = {"channels": [], "count": 0}
        active = {
            "channels": [{
                "channel_id": 100,
                "state": "active",
                "m3u_profile_id": 101,
                "clients": [{"id": "viewer"}],
            }],
            "count": 1,
        }
        fetch = Mock(side_effect=[idle, active])
        self.udi.fetcher._fetch_url = fetch

        self.assertEqual(self.udi._get_proxy_status(), {})
        self.udi._proxy_status_last_fetch = (
            time.time() - self.udi._proxy_status_authoritative_ttl - 0.01
        )
        context = self.udi.get_active_stream_context_per_profile(1)

        self.assertEqual(context[101]['active_streams'], 1)
        self.assertEqual(context[101]['real_viewers'], 1)
        self.assertEqual(fetch.call_count, 2)

    def test_strict_usage_rejects_transport_failure_after_cached_idle(self):
        """A cached zero cannot authorize capacity after status health fails."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        fetch = Mock(
            side_effect=[
                {"channels": [], "count": 0},
                None,
            ]
        )
        self.udi.fetcher._fetch_url = fetch

        self.assertEqual(self.udi._get_proxy_status(), {})
        self.udi._proxy_status_last_fetch = (
            time.time() - self.udi._proxy_status_authoritative_ttl - 0.01
        )
        cached_at = self.udi._proxy_status_last_fetch
        with self.assertRaises(ProxyStatusTransportError):
            self.udi.get_active_stream_context_per_profile(1)

        self.assertEqual(self.udi._proxy_status_cache, {})
        self.assertEqual(self.udi._proxy_status_last_fetch, cached_at)
        self.assertTrue(self.udi._proxy_status_cache_authoritative)
        self.assertEqual(
            self.udi._proxy_status_last_error,
            "proxy_status_transport_failed",
        )

    def test_strict_usage_distinguishes_missing_base_url_from_idle(self):
        """Configuration absence is unknown capacity, not an idle provider."""
        self.udi.fetcher.base_url = None

        with self.assertRaises(ProxyStatusConfigurationError) as raised:
            self.udi.get_active_stream_context_per_profile(1)

        self.assertEqual(raised.exception.reason, "proxy_status_base_url_missing")
        self.assertEqual(
            self.udi._proxy_status_last_error,
            "proxy_status_base_url_missing",
        )
        self.assertFalse(self.udi._proxy_status_cache_authoritative)

    def test_real_fetcher_path_rejects_malformed_status_for_strict_usage(self):
        """Manager capacity uses the fetcher's envelope validation end to end."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        with patch.object(
            self.udi.fetcher,
            '_fetch_url',
            return_value={"channels": [], "count": "0"},
        ):
            with self.assertRaises(ProxyStatusPayloadError) as raised:
                self.udi.get_active_stream_context_per_profile(1)

        self.assertEqual(raised.exception.reason, "proxy_status_count_invalid")
        self.assertFalse(self.udi._proxy_status_cache_authoritative)

    def test_real_fetcher_path_accepts_authoritative_idle(self):
        """The complete empty envelope remains a legitimate zero-usage status."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        with patch.object(
            self.udi.fetcher,
            '_fetch_url',
            return_value={"channels": [], "count": 0},
        ):
            self.assertEqual(
                self.udi.get_active_stream_context_per_profile(1),
                {},
            )

        self.assertTrue(self.udi._proxy_status_cache_authoritative)
        self.assertIsNone(self.udi._proxy_status_last_error)

    def test_public_status_mutation_cannot_corrupt_cached_capacity_authority(self):
        """Nested public snapshots never alias the strict capacity cache."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        self.udi._proxy_status_authoritative_ttl = 60
        payload = {
            "channels": [{
                "channel_id": 100,
                "state": "active",
                "m3u_profile_id": 101,
                "clients": [{"id": "viewer"}],
            }],
            "count": 1,
        }

        with patch.object(self.udi.fetcher, "_fetch_url", return_value=payload):
            public_status = self.udi.get_proxy_status()

        public_status["100"]["m3u_profile_id"] = None
        public_status["100"]["clients"].clear()
        context = self.udi.get_active_stream_context_per_profile(1)

        self.assertEqual(context[101]["active_streams"], 1)
        self.assertEqual(context[101]["real_viewers"], 1)
        self.assertEqual(
            self.udi._proxy_status_cache["100"]["m3u_profile_id"],
            101,
        )
        self.assertEqual(
            self.udi._proxy_status_cache["100"]["clients"],
            [{"id": "viewer"}],
        )

    def test_active_profile_identity_mutations_are_unknown_capacity(self):
        """Set-but-invalid profile IDs cannot disappear from provider usage."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        mutations = (
            ("unknown-profile", "proxy_status_active_profile_id_invalid"),
            (True, "proxy_status_active_profile_id_invalid"),
            (999, "proxy_status_active_profile_unowned"),
        )

        for profile_id, expected_reason in mutations:
            if self.udi._proxy_status_cache_authoritative:
                self.udi._proxy_status_last_fetch = (
                    time.time()
                    - self.udi._proxy_status_authoritative_ttl
                    - 0.01
                )
            payload = {
                "channels": [{
                    "channel_id": "active-channel",
                    "state": "active",
                    "m3u_profile_id": profile_id,
                    "clients": [{"id": "viewer"}],
                }],
                "count": 1,
            }
            for getter_name in (
                "get_active_stream_context_per_profile",
                "get_active_streams_for_account",
            ):
                with self.subTest(profile_id=profile_id, getter=getter_name):
                    with patch.object(
                        self.udi.fetcher,
                        "_fetch_url",
                        return_value=payload,
                    ):
                        with self.assertRaises(ProxyStatusPayloadError) as raised:
                            getattr(self.udi, getter_name)(1)
                    self.assertEqual(raised.exception.reason, expected_reason)

    def test_active_custom_status_without_profile_remains_explicit(self):
        """A genuinely profile-free custom stream consumes no provider profile."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        payload = {
            "channels": [{
                "channel_id": "custom-channel",
                "state": "active",
                "m3u_profile_id": None,
                "clients": [{"id": "viewer"}],
            }],
            "count": 1,
        }

        with patch.object(self.udi.fetcher, "_fetch_url", return_value=payload):
            self.assertEqual(
                self.udi.get_active_stream_context_per_profile(1),
                {},
            )
            self.assertEqual(self.udi.get_active_streams_for_account(1), 0)

    def test_active_upstream_with_empty_clients_still_consumes_profile_slot(self):
        """A transient client gap cannot turn an active upstream into idle."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        payload = {
            "channels": [{
                "channel_id": "active-upstream",
                "state": "active",
                "m3u_profile_id": 101,
                "clients": [],
            }],
            "count": 1,
        }

        with patch.object(self.udi.fetcher, "_fetch_url", return_value=payload):
            context = self.udi.get_active_stream_context_per_profile(1)
            account_count = self.udi.get_active_streams_for_account(1)

        self.assertEqual(context[101]["active_streams"], 1)
        self.assertEqual(context[101]["real_viewers"], 0)
        self.assertEqual(context[101]["real_viewer_streams"], 0)
        self.assertEqual(context[101]["shadow_watchers"], 0)
        self.assertEqual(account_count, 1)

    def test_ten_concurrent_capacity_polls_share_one_short_lived_fetch(self):
        """Concurrent preemption polls cannot amplify into provider overload."""
        payload = {
            "channels": [{
                "channel_id": "active-channel",
                "state": "active",
                "m3u_profile_id": 101,
                "clients": [{"id": "viewer"}],
            }],
            "count": 1,
        }

        results, errors = self._run_concurrent_strict_usage_calls(payload)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 10)
        self.assertTrue(
            all(result[101]["active_streams"] == 1 for result in results)
        )
        self.assertGreater(self.udi._proxy_status_authoritative_ttl, 0)
        self.assertLessEqual(self.udi._proxy_status_authoritative_ttl, 0.1)

    def test_ten_concurrent_capacity_polls_share_and_propagate_failure(self):
        """All joined callers fail closed after one failed provider request."""
        results, errors = self._run_concurrent_strict_usage_calls(None)

        self.assertEqual(results, [])
        self.assertEqual(len(errors), 10)
        self.assertTrue(
            all(isinstance(error, ProxyStatusTransportError) for error in errors)
        )
        self.assertEqual(
            {error.reason for error in errors},
            {"proxy_status_transport_failed"},
        )

    def test_short_failure_snapshot_prevents_instant_retry_storm(self):
        """Even an immediate transport failure is shared for the short bound."""
        self.udi.fetcher.base_url = "http://test-dispatcharr.local"
        fetch = Mock(return_value=None)
        self.udi.fetcher._fetch_url = fetch

        errors = []
        for _index in range(10):
            with self.assertRaises(ProxyStatusTransportError) as raised:
                self.udi.get_active_stream_context_per_profile(1)
            errors.append(raised.exception.reason)

        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(set(errors), {"proxy_status_transport_failed"})
    
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

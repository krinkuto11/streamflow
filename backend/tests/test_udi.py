#!/usr/bin/env python3
"""
Unit tests for the UDI (Universal Data Index) system.

Tests cover:
1. UDI Models
2. UDI Storage
3. UDI Cache
4. UDI Manager
"""

import unittest
import tempfile
import json
import os
import sys
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

# Set up CONFIG_DIR before importing UDI modules
os.environ['CONFIG_DIR'] = tempfile.mkdtemp()

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.udi.models import Channel, Stream, ChannelGroup, Logo, M3UAccount, UDIMetadata
from apps.udi.storage import UDIStorage
from apps.udi.cache import UDICache
from apps.udi.fetcher import FetchResult
from apps.udi.manager import UDIManager


class FakeSettingsDb:
    def __init__(self, settings=None):
        self.settings = settings or {}

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)


class TestModels(unittest.TestCase):
    """Test UDI data models."""
    
    def test_channel_from_dict(self):
        """Test creating a Channel from a dictionary."""
        data = {
            'id': 1,
            'name': 'Test Channel',
            'channel_number': 100,
            'streams': [1, 2, 3]
        }
        channel = Channel.from_dict(data)
        
        self.assertEqual(channel.id, 1)
        self.assertEqual(channel.name, 'Test Channel')
        self.assertEqual(channel.channel_number, 100)
        self.assertEqual(channel.streams, [1, 2, 3])
    
    def test_channel_to_dict(self):
        """Test converting a Channel to a dictionary."""
        channel = Channel(id=1, name='Test Channel', streams=[1, 2])
        result = channel.to_dict()
        
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['name'], 'Test Channel')
        self.assertEqual(result['streams'], [1, 2])
    
    def test_stream_from_dict(self):
        """Test creating a Stream from a dictionary."""
        data = {
            'id': 1,
            'name': 'Test Stream',
            'url': 'http://test.com/stream',
            'm3u_account': 5,
            'is_custom': False
        }
        stream = Stream.from_dict(data)
        
        self.assertEqual(stream.id, 1)
        self.assertEqual(stream.name, 'Test Stream')
        self.assertEqual(stream.url, 'http://test.com/stream')
        self.assertEqual(stream.m3u_account, 5)
        self.assertFalse(stream.is_custom)
    
    def test_stream_to_dict(self):
        """Test converting a Stream to a dictionary."""
        stream = Stream(id=1, name='Test Stream', url='http://test.com')
        result = stream.to_dict()
        
        self.assertEqual(result['id'], 1)
        self.assertEqual(result['name'], 'Test Stream')
        self.assertEqual(result['url'], 'http://test.com')
    
    def test_channel_group_from_dict(self):
        """Test creating a ChannelGroup from a dictionary."""
        data = {
            'id': 1,
            'name': 'Sports',
            'channel_count': 10
        }
        group = ChannelGroup.from_dict(data)
        
        self.assertEqual(group.id, 1)
        self.assertEqual(group.name, 'Sports')
        self.assertEqual(group.channel_count, 10)
    
    def test_m3u_account_from_dict(self):
        """Test creating an M3UAccount from a dictionary."""
        data = {
            'id': 1,
            'name': 'Main Account',
            'is_active': True,
            'max_streams': 5
        }
        account = M3UAccount.from_dict(data)
        
        self.assertEqual(account.id, 1)
        self.assertEqual(account.name, 'Main Account')
        self.assertTrue(account.is_active)
        self.assertEqual(account.max_streams, 5)


class TestUDIStorage(unittest.TestCase):
    """Test UDI Storage class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.storage = UDIStorage(storage_dir=Path(self.temp_dir) / 'udi')
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_save_and_load_channels(self):
        """Test saving and loading channels."""
        channels = [
            {'id': 1, 'name': 'Channel 1'},
            {'id': 2, 'name': 'Channel 2'}
        ]
        
        self.assertTrue(self.storage.save_channels(channels))
        loaded = self.storage.load_channels()
        
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]['name'], 'Channel 1')
    
    def test_save_and_load_streams(self):
        """Test saving and loading streams."""
        streams = [
            {'id': 1, 'name': 'Stream 1', 'url': 'http://test.com/1'},
            {'id': 2, 'name': 'Stream 2', 'url': 'http://test.com/2'}
        ]
        
        self.assertTrue(self.storage.save_streams(streams))
        loaded = self.storage.load_streams()
        
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]['url'], 'http://test.com/1')
    
    def test_get_channel_by_id(self):
        """Test getting a specific channel by ID."""
        channels = [
            {'id': 1, 'name': 'Channel 1'},
            {'id': 2, 'name': 'Channel 2'}
        ]
        self.storage.save_channels(channels)
        
        channel = self.storage.get_channel_by_id(2)
        self.assertIsNotNone(channel)
        self.assertEqual(channel['name'], 'Channel 2')
        
        # Test non-existent channel
        self.assertIsNone(self.storage.get_channel_by_id(999))
    
    def test_update_channel(self):
        """Test updating a specific channel."""
        channels = [
            {'id': 1, 'name': 'Channel 1'},
            {'id': 2, 'name': 'Channel 2'}
        ]
        self.storage.save_channels(channels)
        
        # Update existing channel
        updated = {'id': 2, 'name': 'Updated Channel 2'}
        self.assertTrue(self.storage.update_channel(2, updated))
        
        channel = self.storage.get_channel_by_id(2)
        self.assertEqual(channel['name'], 'Updated Channel 2')
    
    def test_clear_all(self):
        """Test clearing all stored data."""
        self.storage.save_channels([{'id': 1, 'name': 'Test'}])
        self.storage.save_streams([{'id': 1, 'name': 'Test'}])
        
        self.assertTrue(self.storage.clear_all())
        
        self.assertEqual(self.storage.load_channels(), [])
        self.assertEqual(self.storage.load_streams(), [])
        self.assertEqual(list(self.storage.storage_dir.glob("*.last-good")), [])
    
    def test_is_initialized(self):
        """Test checking if storage is initialized."""
        self.assertFalse(self.storage.is_initialized())
        
        self.storage.save_channels([{'id': 1}])
        self.assertTrue(self.storage.is_initialized())


class TestUDICache(unittest.TestCase):
    """Test UDI Cache class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.cache = UDICache(
            channels_ttl=60,
            streams_ttl=60,
            channel_groups_ttl=120,
            logos_ttl=120,
            m3u_accounts_ttl=120
        )
    
    def test_mark_refreshed(self):
        """Test marking entity as refreshed."""
        self.assertFalse(self.cache.is_valid('channels'))
        
        self.cache.mark_refreshed('channels')
        self.assertTrue(self.cache.is_valid('channels'))
    
    def test_invalidate(self):
        """Test invalidating cache."""
        self.cache.mark_refreshed('channels')
        self.assertTrue(self.cache.is_valid('channels'))
        
        self.cache.invalidate('channels')
        self.assertFalse(self.cache.is_valid('channels'))
    
    def test_invalidate_all(self):
        """Test invalidating all caches."""
        self.cache.mark_refreshed('channels')
        self.cache.mark_refreshed('streams')
        
        self.cache.invalidate_all()
        
        self.assertFalse(self.cache.is_valid('channels'))
        self.assertFalse(self.cache.is_valid('streams'))
    
    def test_cache_expiry(self):
        """Test cache TTL expiry."""
        # Mark as refreshed with a past timestamp
        past_time = datetime.now() - timedelta(seconds=120)
        self.cache.mark_refreshed('channels', past_time)
        
        # Should be expired (TTL is 60 seconds)
        self.assertFalse(self.cache.is_valid('channels'))
        self.assertTrue(self.cache.needs_refresh('channels'))
    
    def test_get_time_until_expiry(self):
        """Test getting time until expiry."""
        # Never refreshed
        self.assertIsNone(self.cache.get_time_until_expiry('channels'))
        
        # Just refreshed
        self.cache.mark_refreshed('channels')
        time_left = self.cache.get_time_until_expiry('channels')
        self.assertIsNotNone(time_left)
        self.assertGreater(time_left, 50)  # Should be close to 60
    
    def test_get_status(self):
        """Test getting cache status."""
        self.cache.mark_refreshed('channels')
        
        status = self.cache.get_status()
        
        self.assertIn('channels', status)
        self.assertTrue(status['channels']['is_valid'])
        self.assertFalse(status['channels']['invalidated'])


class TestUDIManager(unittest.TestCase):
    """Test UDI Manager class."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        os.environ['CONFIG_DIR'] = self.temp_dir
        
        # Create a fresh manager for each test
        self.manager = UDIManager()
        self.manager.storage = UDIStorage(storage_dir=Path(self.temp_dir) / 'udi')
    
    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
    
    def test_initial_state(self):
        """Test initial manager state."""
        self.assertFalse(self.manager.is_initialized())
    
    def test_get_channels_empty(self):
        """Test getting channels when empty."""
        # Manually set initialized to avoid API calls
        self.manager._initialized = True
        channels = self.manager.get_channels()
        self.assertEqual(channels, [])
    
    def test_get_streams_empty(self):
        """Test getting streams when empty."""
        self.manager._initialized = True
        streams = self.manager.get_streams(log_result=False)
        self.assertEqual(streams, [])
    
    def test_build_indexes(self):
        """Test building index caches."""
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1'},
            {'id': 2, 'name': 'Channel 2'}
        ]
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1', 'url': 'http://test.com/1'},
            {'id': 20, 'name': 'Stream 2', 'url': 'http://test.com/2'}
        ]
        
        self.manager._build_indexes()
        
        self.assertEqual(len(self.manager._channels_by_id), 2)
        self.assertEqual(len(self.manager._streams_by_id), 2)
        self.assertEqual(len(self.manager._streams_by_url), 2)
        self.assertEqual(len(self.manager._valid_stream_ids), 2)

    def test_account_authority_lease_blocks_refresh_pointer_swap(self):
        """A refresh cannot replace account authority during final admission."""

        class ObservedRLock:
            def __init__(self):
                self._inner = threading.RLock()
                self.contention = threading.Event()

            def acquire(self, blocking=True, timeout=-1):
                if self._inner.acquire(blocking=False):
                    return True
                self.contention.set()
                if not blocking:
                    return False
                if timeout == -1:
                    return self._inner.acquire()
                return self._inner.acquire(timeout=timeout)

            def release(self):
                self._inner.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, _exc_type, _exc, _tb):
                self.release()

        old_account = {
            "id": 7,
            "name": "Before refresh",
            "profiles": [{"id": 70, "max_streams": 1}],
        }
        new_account = {
            "id": 7,
            "name": "After refresh",
            "profiles": [{"id": 70, "max_streams": 2}],
        }
        self.manager._initialized = True
        self.manager._m3u_accounts_cache = [old_account]
        self.manager._build_indexes()
        observed_lock = ObservedRLock()
        self.manager._lock = observed_lock
        self.manager.fetcher.fetch_m3u_accounts = Mock(return_value=[new_account])

        lease_entered = threading.Event()
        release_lease = threading.Event()
        refresh_finished = threading.Event()
        snapshots = []
        refresh_results = []
        thread_errors = []

        def hold_lease():
            try:
                with self.manager.account_authority_lease(7) as snapshot:
                    snapshots.append(snapshot)
                    lease_entered.set()
                    if not release_lease.wait(timeout=2):
                        raise AssertionError("lease release timed out")
            except BaseException as exc:  # surfaced in the parent test thread
                thread_errors.append(exc)

        def refresh_accounts():
            try:
                refresh_results.append(self.manager.refresh_m3u_accounts())
            except BaseException as exc:  # surfaced in the parent test thread
                thread_errors.append(exc)
            finally:
                refresh_finished.set()

        holder = threading.Thread(target=hold_lease)
        refresher = threading.Thread(target=refresh_accounts)
        holder.start()
        self.assertTrue(lease_entered.wait(timeout=2))
        refresher.start()

        self.assertTrue(observed_lock.contention.wait(timeout=2))
        self.assertFalse(refresh_finished.is_set())
        self.assertEqual(snapshots[0]["name"], "Before refresh")
        self.assertEqual(snapshots[0]["profiles"][0]["max_streams"], 1)

        release_lease.set()
        holder.join(timeout=2)
        refresher.join(timeout=2)

        self.assertFalse(holder.is_alive())
        self.assertFalse(refresher.is_alive())
        self.assertEqual(thread_errors, [])
        self.assertEqual(refresh_results, [True])
        self.assertEqual(
            self.manager.get_m3u_account_by_id(7)["name"],
            "After refresh",
        )
        self.assertIs(UDIManager.supports_account_authority_lease, True)
    
    def test_get_channel_by_id(self):
        """Test getting channel by ID."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1'},
            {'id': 2, 'name': 'Channel 2'}
        ]
        self.manager._build_indexes()
        
        channel = self.manager.get_channel_by_id(2)
        self.assertIsNotNone(channel)
        self.assertEqual(channel['name'], 'Channel 2')
        
        # Non-existent channel with fetch_if_missing=False
        self.assertIsNone(self.manager.get_channel_by_id(999, fetch_if_missing=False))
    
    def test_get_channel_by_id_fetch_if_missing(self):
        """Test that get_channel_by_id fetches from API when channel not in cache."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1'}
        ]
        self.manager._build_indexes()
        
        # Mock the fetcher's fetch_channel_by_id method
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.return_value = {'id': 999, 'name': 'Fetched Channel'}
        
        # Channel not in cache, should fetch from API
        channel = self.manager.get_channel_by_id(999)
        
        self.assertIsNotNone(channel)
        self.assertEqual(channel['id'], 999)
        self.assertEqual(channel['name'], 'Fetched Channel')
        
        # Verify it was added to cache
        self.assertIn(999, self.manager._channels_by_id)
        self.assertTrue(any(ch.get('id') == 999 for ch in self.manager._channels_cache))
        
        # Verify API was called
        self.manager.fetcher.fetch_channel_by_id.assert_called_once_with(999)
    
    def test_get_channel_by_id_fetch_if_missing_api_returns_none(self):
        """Test that get_channel_by_id returns None when API also returns None."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1'}
        ]
        self.manager._build_indexes()
        
        # Mock the fetcher to return None (channel doesn't exist)
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.return_value = None
        
        # Channel not in cache and not in API
        channel = self.manager.get_channel_by_id(999)
        
        self.assertIsNone(channel)
        self.manager.fetcher.fetch_channel_by_id.assert_called_once_with(999)
    
    def test_get_channel_by_id_fetch_if_missing_api_error(self):
        """Test that get_channel_by_id handles API errors gracefully."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1'}
        ]
        self.manager._build_indexes()
        
        # Mock the fetcher to raise an exception
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.side_effect = Exception("API Error")
        
        # Should return None on error
        channel = self.manager.get_channel_by_id(999)
        
        self.assertIsNone(channel)
    
    def test_get_stream_by_id(self):
        """Test getting stream by ID."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1'},
            {'id': 20, 'name': 'Stream 2'}
        ]
        self.manager._build_indexes()
        
        stream = self.manager.get_stream_by_id(20)
        self.assertIsNotNone(stream)
        self.assertEqual(stream['name'], 'Stream 2')
    
    def test_get_stream_by_url(self):
        """Test getting stream by URL."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1', 'url': 'http://test.com/1'},
            {'id': 20, 'name': 'Stream 2', 'url': 'http://test.com/2'}
        ]
        self.manager._build_indexes()
        
        stream = self.manager.get_stream_by_url('http://test.com/2')
        self.assertIsNotNone(stream)
        self.assertEqual(stream['name'], 'Stream 2')
    
    def test_get_valid_stream_ids(self):
        """Test getting valid stream IDs."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1'},
            {'id': 20, 'name': 'Stream 2'},
            {'id': 30, 'name': 'Stream 3'}
        ]
        self.manager._build_indexes()
        
        valid_ids = self.manager.get_valid_stream_ids()
        self.assertEqual(valid_ids, {10, 20, 30})
    
    def test_has_custom_streams_true(self):
        """Test has_custom_streams when custom streams exist."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1', 'is_custom': False},
            {'id': 20, 'name': 'Custom Stream', 'is_custom': True}
        ]
        
        self.assertTrue(self.manager.has_custom_streams())
    
    def test_has_custom_streams_false(self):
        """Test has_custom_streams when no custom streams exist."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1', 'is_custom': False},
            {'id': 20, 'name': 'Stream 2', 'is_custom': False}
        ]
        
        self.assertFalse(self.manager.has_custom_streams())
    
    def test_get_channel_streams(self):
        """Test getting streams for a channel."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Channel 1', 'streams': [10, 20]}
        ]
        self.manager._streams_cache = [
            {'id': 10, 'name': 'Stream 1'},
            {'id': 20, 'name': 'Stream 2'},
            {'id': 30, 'name': 'Stream 3'}
        ]
        self.manager._build_indexes()
        
        streams = self.manager.get_channel_streams(1)
        self.assertEqual(len(streams), 2)
        
        # Channel with no streams
        self.manager._channels_cache.append({'id': 2, 'name': 'Empty Channel', 'streams': []})
        self.manager._build_indexes()
        streams = self.manager.get_channel_streams(2)
        self.assertEqual(streams, [])
        
        # Non-existent channel
        streams = self.manager.get_channel_streams(999)
        self.assertEqual(streams, [])
    
    def test_get_status(self):
        """Test getting manager status."""
        self.manager._initialized = True
        self.manager._channels_cache = [{'id': 1}]
        self.manager._streams_cache = [{'id': 1}, {'id': 2}]
        
        status = self.manager.get_status()
        
        self.assertTrue(status['initialized'])
        self.assertFalse(status['background_refresh_running'])
        self.assertEqual(status['data_counts']['channels'], 1)
        self.assertEqual(status['data_counts']['streams'], 2)
    
    def test_update_channel(self):
        """Test updating a channel in cache."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Original Name'}
        ]
        self.manager._build_indexes()
        
        updated_data = {'id': 1, 'name': 'Updated Name'}
        result = self.manager.update_channel(1, updated_data)
        
        self.assertTrue(result)
        channel = self.manager.get_channel_by_id(1)
        self.assertEqual(channel['name'], 'Updated Name')
    
    def test_update_stream(self):
        """Test updating a stream in cache."""
        self.manager._initialized = True
        self.manager._streams_cache = [
            {'id': 1, 'name': 'Original', 'url': 'http://old.com'}
        ]
        self.manager._build_indexes()
        
        updated_data = {'id': 1, 'name': 'Updated', 'url': 'http://new.com'}
        result = self.manager.update_stream(1, updated_data)
        
        self.assertTrue(result)
        stream = self.manager.get_stream_by_id(1)
        self.assertEqual(stream['name'], 'Updated')
        self.assertEqual(stream['url'], 'http://new.com')
    
    def test_refresh_channel_by_id(self):
        """Test refreshing a single channel by ID."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Original Channel', 'streams': [10]}
        ]
        self.manager._build_indexes()
        
        # Mock the fetcher to return updated channel data
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.return_value = {
            'id': 1, 
            'name': 'Updated Channel', 
            'streams': [10, 20, 30]
        }
        
        # Refresh the channel
        result = self.manager.refresh_channel_by_id(1)
        
        self.assertTrue(result)
        self.manager.fetcher.fetch_channel_by_id.assert_called_once_with(1)
        
        # Verify the channel was updated in cache
        channel = self.manager.get_channel_by_id(1)
        self.assertEqual(channel['name'], 'Updated Channel')
        self.assertEqual(channel['streams'], [10, 20, 30])
    
    def test_refresh_channel_by_id_not_found(self):
        """Test refreshing a channel that doesn't exist."""
        self.manager._initialized = True
        self.manager._channels_cache = []
        self.manager._build_indexes()
        
        # Mock the fetcher to return None (channel not found)
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.return_value = None
        
        # Try to refresh non-existent channel
        result = self.manager.refresh_channel_by_id(999)
        
        self.assertFalse(result)
        self.manager.fetcher.fetch_channel_by_id.assert_called_once_with(999)
    
    def test_refresh_channel_by_id_error(self):
        """Test handling errors during channel refresh."""
        self.manager._initialized = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Test Channel'}
        ]
        self.manager._build_indexes()
        
        # Mock the fetcher to raise an exception
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channel_by_id.side_effect = Exception("API Error")
        
        # Should return False on error
        result = self.manager.refresh_channel_by_id(1)
        
        self.assertFalse(result)

    def test_refresh_all_preserves_existing_cache_on_empty_unknown_fetch(self):
        """A transient fetch failure must not replace a good cache with empties."""
        self.manager._initialized = True
        self.manager._network_ready = True
        self.manager._channels_cache = [{'id': 1, 'name': 'Existing Channel'}]
        self.manager._streams_cache = [{'id': 10, 'name': 'Existing Stream'}]
        self.manager._build_indexes()

        self.manager.fetcher = Mock()
        self.manager.fetcher.refresh_config.return_value = None
        self.manager.fetcher.fetch_all_ids.return_value = {}
        self.manager.fetcher.fetch_channels.return_value = FetchResult()
        self.manager.fetcher.fetch_streams.return_value = FetchResult()
        self.manager.fetcher.fetch_channel_groups.return_value = []
        self.manager.fetcher.fetch_logos.return_value = FetchResult()
        self.manager.fetcher.fetch_m3u_accounts.return_value = []
        self.manager.fetcher.fetch_channel_profiles.return_value = []

        with patch('apps.udi.manager.get_dispatcharr_config') as mock_config:
            mock_config.return_value.is_configured.return_value = True
            result = self.manager.refresh_all()

        self.assertFalse(result)
        self.assertEqual(self.manager._channels_cache, [{'id': 1, 'name': 'Existing Channel'}])
        self.assertEqual(self.manager._streams_cache, [{'id': 10, 'name': 'Existing Stream'}])
        self.assertTrue(self.manager.is_network_ready())

    def test_refresh_streams_preserves_existing_cache_on_partial_fetch(self):
        """A page timeout during refresh_streams must not replace a good cache."""
        existing_stream = {'id': 10, 'name': 'Existing Stream', 'url': 'http://x/old'}
        self.manager._initialized = True
        self.manager._streams_cache = [existing_stream]
        self.manager._build_indexes()

        partial_streams = [
            {'id': idx, 'name': f'Partial {idx}', 'url': f'http://x/{idx}'}
            for idx in range(1, 11)
        ]
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_streams.return_value = FetchResult(
            items=partial_streams,
            expected_count=20,
        )

        result = self.manager.refresh_streams()

        self.assertFalse(result)
        self.assertEqual(self.manager._streams_cache, [existing_stream])
        self.assertEqual(self.manager._streams_by_id, {10: existing_stream})

    def test_refresh_streams_allows_confirmed_empty_dispatcharr(self):
        """A real zero-count stream response may replace the stream cache."""
        self.manager._initialized = True
        self.manager._streams_cache = [{'id': 10, 'name': 'Existing Stream'}]
        self.manager._build_indexes()

        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_streams.return_value = FetchResult(
            items=[],
            expected_count=0,
        )

        result = self.manager.refresh_streams()

        self.assertTrue(result)
        self.assertEqual(self.manager._streams_cache, [])
        self.assertEqual(self.manager._streams_by_id, {})

    def test_refresh_all_allows_confirmed_empty_dispatcharr(self):
        """A real zero-count Dispatcharr response remains valid."""
        self.manager.fetcher = Mock()
        self.manager.fetcher.refresh_config.return_value = None
        self.manager.fetcher.fetch_all_ids.return_value = {'channels': set(), 'streams': set()}
        self.manager.fetcher.fetch_channels.return_value = FetchResult(items=[], expected_count=0)
        self.manager.fetcher.fetch_streams.return_value = FetchResult(items=[], expected_count=0)
        self.manager.fetcher.fetch_channel_groups.return_value = []
        self.manager.fetcher.fetch_logos.return_value = FetchResult(items=[], expected_count=0)
        self.manager.fetcher.fetch_m3u_accounts.return_value = []
        self.manager.fetcher.fetch_channel_profiles.return_value = []

        with patch('apps.udi.manager.get_dispatcharr_config') as mock_config:
            mock_config.return_value.is_configured.return_value = True
            result = self.manager.refresh_all()

        self.assertTrue(result)
        self.assertEqual(self.manager._channels_cache, [])
        self.assertEqual(self.manager._streams_cache, [])
        self.assertTrue(self.manager.is_network_ready())

    def test_rehydrate_managed_hidden_channels_fetches_missing_ids(self):
        """StreamFlow-owned hidden channels remain visible to UDI if list APIs omit them."""
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_channels_by_ids.return_value = [
            {'id': 42, 'name': 'Hidden News', 'hidden_from_output': True},
        ]
        fake_db = FakeSettingsDb({
            "streamflow_channel_visibility_state": {
                "42": {"hidden_by": "streamflow", "channel_id": 42},
            },
        })

        with patch('apps.database.manager.get_db_manager', return_value=fake_db):
            channels, count = self.manager._rehydrate_managed_hidden_channels([
                {'id': 1, 'name': 'Visible Channel'},
            ])

        self.assertEqual(count, 1)
        self.assertEqual({channel['id'] for channel in channels}, {1, 42})
        self.manager.fetcher.fetch_channels_by_ids.assert_called_once_with([42])

    def test_refresh_all_rehydrates_hidden_channels_from_ids_oracle_without_state(self):
        """Hidden channels remain in UDI after a reinstall when /ids/?all exposes them."""
        self.manager.fetcher = Mock()
        self.manager.fetcher.refresh_config.return_value = None
        self.manager.fetcher.fetch_all_ids.return_value = {
            'channels': {1, 42},
            'streams': set(),
        }
        self.manager.fetcher.fetch_channels.return_value = FetchResult(
            items=[{'id': 1, 'name': 'Visible Channel', 'streams': []}],
            expected_count=1,
        )
        self.manager.fetcher.fetch_channels_by_ids.return_value = [
            {'id': 42, 'name': 'Hidden News', 'hidden_from_output': True, 'streams': []},
        ]
        self.manager.fetcher.fetch_streams.return_value = FetchResult(items=[], expected_count=0)
        self.manager.fetcher.fetch_channel_groups.return_value = []
        self.manager.fetcher.fetch_logos.return_value = FetchResult(items=[], expected_count=0)
        self.manager.fetcher.fetch_m3u_accounts.return_value = []
        self.manager.fetcher.fetch_channel_profiles.return_value = [
            {'id': 7, 'name': 'News Profile', 'channels': [42]},
        ]

        with patch('apps.udi.manager.get_dispatcharr_config') as mock_config:
            mock_config.return_value.is_configured.return_value = True
            result = self.manager.refresh_all()

        self.assertTrue(result)
        self.assertTrue(self.manager.is_initialized())
        self.assertEqual({channel['id'] for channel in self.manager.get_channels()}, {1, 42})
        self.assertEqual(self.manager.get_channel_by_id(42)['name'], 'Hidden News')
        self.assertEqual(self.manager.get_profile_channels(7)['channels'], [42])
        entity_counts = self.manager.get_init_progress()['entity_counts']
        self.assertEqual(entity_counts['channels']['received'], 2)
        self.assertEqual(entity_counts['channels']['expected'], 2)
        self.assertEqual(entity_counts['channels']['rehydrated_missing'], 1)
        self.manager.fetcher.fetch_channels_by_ids.assert_called_once_with([42])

    def test_delta_refresh_keeps_streamflow_hidden_channel_omitted_by_ids(self):
        """Older Dispatcharr IDs responses must not delete StreamFlow-hidden channels from UDI."""
        hidden_channel = {'id': 42, 'name': 'Old Hidden', 'hidden_from_output': True, 'streams': []}
        self.manager._initialized = True
        self.manager._network_ready = True
        self.manager._channels_cache = [
            {'id': 1, 'name': 'Visible Channel', 'streams': []},
            hidden_channel,
        ]
        self.manager._streams_cache = []
        self.manager._build_indexes()
        self.manager.fetcher = Mock()
        self.manager.fetcher.fetch_all_ids.return_value = {'channels': {1}, 'streams': set()}
        self.manager.fetcher.fetch_channels_by_ids.return_value = [
            {'id': 42, 'name': 'Hidden News', 'hidden_from_output': True, 'streams': []},
        ]
        fake_db = FakeSettingsDb({
            "streamflow_channel_visibility_state": {
                "42": {"hidden_by": "streamflow", "channel_id": 42},
            },
        })

        with patch('apps.udi.manager.get_dispatcharr_config') as mock_config, \
                patch('apps.database.manager.get_db_manager', return_value=fake_db):
            mock_config.return_value.is_configured.return_value = True
            result = self.manager.refresh_delta()

        self.assertTrue(result)
        self.assertIn(42, self.manager._channels_by_id)
        self.assertEqual(self.manager._channels_by_id[42]['name'], 'Hidden News')
        self.manager.fetcher.fetch_channels_by_ids.assert_called_once_with([42])


if __name__ == '__main__':
    unittest.main()

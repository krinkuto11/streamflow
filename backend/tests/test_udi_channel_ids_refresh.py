"""Channel-only UDI refresh keeps hidden channels without reading stream IDs."""

from unittest.mock import Mock, patch

from apps.udi.fetcher import FetchResult, UDIFetcher
from apps.udi.manager import UDIManager


def test_channel_ids_fetch_uses_only_channel_visibility_oracle():
    fetcher = UDIFetcher.__new__(UDIFetcher)
    fetcher.base_url = 'http://dispatcharr.invalid'
    fetcher._fetch_url = Mock(return_value=[1, '42'])

    assert fetcher.fetch_channel_ids() == {1, 42}
    fetcher._fetch_url.assert_called_once_with(
        'http://dispatcharr.invalid/api/channels/channels/ids/?visibility_filter=all'
    )


def test_channel_ids_fetch_rejects_invalid_payload():
    fetcher = UDIFetcher.__new__(UDIFetcher)
    fetcher.base_url = 'http://dispatcharr.invalid'
    fetcher._fetch_url = Mock(return_value=[1, 'bad-id'])

    assert fetcher.fetch_channel_ids() is None


def _manager_with_cached_stream_and_channels():
    manager = UDIManager()
    manager._initialized = True
    manager._network_ready = True
    manager._streams_cache = [
        {'id': 101, 'url': 'http://example.invalid/101', 'm3u_account_id': 5}
    ]
    manager._channel_groups_cache = [
        {'id': 7, 'name': 'News'},
        {'id': 8, 'name': 'Sports'},
    ]
    manager._channels_cache = [
        {'id': 1, 'name': 'Old News', 'channel_group_id': 7, 'streams': [101]},
        {'id': 2, 'name': 'Deleted', 'channel_group_id': 7, 'streams': []},
        {'id': 42, 'name': 'Old Hidden', 'channel_group_id': 7,
         'hidden_from_output': True, 'streams': []},
    ]
    manager._build_indexes()
    manager.fetcher = Mock()
    manager.fetcher.fetch_channels.return_value = FetchResult(
        items=[
            {'id': 1, 'name': 'New News', 'channel_group_id': 7, 'streams': [101]},
            {'id': 3, 'name': 'New Sports', 'channel_group_id': 8, 'streams': []},
        ],
        expected_count=2,
    )
    manager.fetcher.fetch_channels_by_ids.return_value = [
        {'id': 42, 'name': 'Fresh Hidden', 'channel_group_id': 7,
         'hidden_from_output': True, 'streams': []},
    ]
    return manager


def _assert_refresh_read_parity(manager):
    assert {channel['id'] for channel in manager.get_channels()} == {1, 3, 42}
    assert manager.get_channel_by_id(1, fetch_if_missing=False)['name'] == 'New News'
    assert manager.get_channel_by_id(2, fetch_if_missing=False) is None
    assert manager.get_channel_by_id(3, fetch_if_missing=False)['name'] == 'New Sports'
    assert manager.get_channel_by_id(42, fetch_if_missing=False)['name'] == 'Fresh Hidden'
    assert [channel['id'] for channel in manager.get_channels_by_group(7)] == [1, 42]
    assert [channel['id'] for channel in manager.get_channels_by_group(8)] == [3]
    assert [stream['id'] for stream in manager.get_channel_streams(1)] == [101]
    assert manager.get_stream_by_id(101)['m3u_account_id'] == 5
    assert manager.get_valid_stream_ids() == {101}


def test_refresh_channels_rehydrates_hidden_channel_from_ids_oracle():
    manager = _manager_with_cached_stream_and_channels()
    manager.fetcher.fetch_channel_ids.return_value = {1, 3, 42}
    manager._get_managed_hidden_channel_ids = Mock(return_value=set())

    with patch('apps.udi.manager.get_dispatcharr_config') as config:
        config.return_value.is_configured.return_value = True
        assert manager.refresh_channels() is True

    _assert_refresh_read_parity(manager)
    manager.fetcher.fetch_channel_ids.assert_called_once_with()
    manager.fetcher.fetch_all_ids.assert_not_called()
    manager.fetcher.fetch_channels_by_ids.assert_called_once_with([42])


def test_refresh_channels_rehydrates_managed_hidden_when_ids_omit_it():
    manager = _manager_with_cached_stream_and_channels()
    manager.fetcher.fetch_channel_ids.return_value = {1, 3}
    manager._get_managed_hidden_channel_ids = Mock(return_value={42})

    with patch('apps.udi.manager.get_dispatcharr_config') as config:
        config.return_value.is_configured.return_value = True
        assert manager.refresh_channels() is True

    _assert_refresh_read_parity(manager)
    manager.fetcher.fetch_channel_ids.assert_called_once_with()
    manager.fetcher.fetch_all_ids.assert_not_called()
    manager.fetcher.fetch_channels_by_ids.assert_called_once_with([42])

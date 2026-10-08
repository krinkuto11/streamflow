"""Session creation reuses only sufficiently fresh live UDI stream data."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from apps.stream.stream_session_manager import StreamSessionManager


def _manager_and_udi():
    manager = object.__new__(StreamSessionManager)
    manager.sessions = {}
    manager.session_locks = {}
    manager.scoring_windows = {}
    manager.channel_ownership = {}
    manager._last_streams_refresh = 0
    manager._save_sessions = Mock(return_value=True)

    udi = Mock()
    udi.get_channel_by_id.side_effect = lambda channel_id: {
        'id': channel_id, 'name': f'Channel {channel_id}',
    }
    udi.is_network_ready.return_value = True
    udi.cache.is_valid.return_value = True
    udi.get_cache_last_refresh.return_value = None
    udi.refresh_streams.return_value = True
    return manager, udi


def test_recent_live_udi_stream_cache_avoids_initial_full_fetch():
    manager, udi = _manager_and_udi()
    udi.get_cache_last_refresh.return_value = datetime.now() - timedelta(seconds=5)

    with patch('apps.stream.stream_session_manager.get_udi_manager', return_value=udi):
        session_id = manager.create_session(1)

    assert session_id in manager.sessions
    udi.cache.is_valid.assert_called_once_with('streams')
    udi.refresh_streams.assert_not_called()


def test_stale_cache_fetches_updated_metadata_and_new_streams():
    manager, udi = _manager_and_udi()
    udi.get_cache_last_refresh.return_value = datetime.now() - timedelta(seconds=31)
    stream_data = [
        {'id': 11, 'name': 'Old name', 'url': 'http://old.invalid'},
    ]

    def full_refresh():
        stream_data[:] = [
            {'id': 11, 'name': 'Updated name', 'url': 'http://new.invalid'},
            {'id': 12, 'name': 'New source', 'url': 'http://added.invalid'},
        ]
        return True

    udi.refresh_streams.side_effect = full_refresh
    udi.get_streams.side_effect = lambda: list(stream_data)
    with patch('apps.stream.stream_session_manager.get_udi_manager', return_value=udi):
        session_id = manager.create_session(1, regex_filter='Updated|New')
        manager._discover_streams(session_id)

    udi.refresh_streams.assert_called_once_with()
    assert set(manager.sessions[session_id].streams) == {11, 12}
    assert manager.sessions[session_id].streams[11].url == 'http://new.invalid'


def test_recent_sql_only_cache_does_not_skip_live_refresh():
    manager, udi = _manager_and_udi()
    udi.is_network_ready.return_value = False
    udi.get_cache_last_refresh.return_value = datetime.now()

    with patch('apps.stream.stream_session_manager.get_udi_manager', return_value=udi):
        manager.create_session(1)

    udi.refresh_streams.assert_called_once_with()


def test_long_fetch_starts_cooldown_after_completion():
    manager, udi = _manager_and_udi()
    clock = [1000.0]

    def slow_refresh():
        clock[0] += 107.0
        return True

    udi.refresh_streams.side_effect = slow_refresh
    with patch('apps.stream.stream_session_manager.get_udi_manager', return_value=udi), \
         patch('apps.stream.stream_session_manager.time.time', side_effect=lambda: clock[0]):
        manager.create_session(1)
        assert manager._last_streams_refresh == 1107.0
        manager.create_session(2)

    assert udi.refresh_streams.call_count == 1


def test_concurrent_session_creation_shares_one_full_fetch():
    manager, udi = _manager_and_udi()
    both_started = threading.Barrier(2)
    udi.refresh_channel_by_id.side_effect = lambda _channel_id: both_started.wait(timeout=5)

    with patch('apps.stream.stream_session_manager.get_udi_manager', return_value=udi):
        with ThreadPoolExecutor(max_workers=2) as executor:
            sessions = list(executor.map(manager.create_session, [1, 2]))

    assert len(set(sessions)) == 2
    assert udi.refresh_streams.call_count == 1

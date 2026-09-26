"""Quarantine and revive must report Dispatcharr write failures accurately."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from flask import Flask

from apps.api.stream_sessions_handlers import (
    quarantine_stream_response,
    revive_stream_response,
)
from apps.database.manager import get_db_manager
from apps.stream.dead_streams_tracker import DeadStreamsTracker
from apps.stream.stream_monitoring_service import StreamMonitoringService
from apps.stream.stream_session_manager import SessionInfo, StreamInfo, StreamSessionManager


def _manager_with_stream(*, status='stable', reason=None):
    manager = StreamSessionManager.__new__(StreamSessionManager)
    stream = StreamInfo(
        stream_id=101, url='http://example.invalid/stream', name='Source',
        channel_id=9, status=status, reliability_score=80,
    )
    stream.status_reason = reason
    session = SessionInfo(
        session_id='session-9', channel_id=9, channel_name='Channel',
        regex_filter='.*', created_at=time.time(), is_active=True,
        streams={101: stream},
    )
    if status == 'quarantined':
        session.quarantined_stream_ids.add(101)
    manager.sessions = {session.session_id: session}
    manager.session_locks = {session.session_id: threading.Lock()}
    manager._save_sessions = Mock()
    return manager, session, stream


def test_manual_quarantine_requires_confirmed_dispatcharr_removal():
    manager, session, stream = _manager_with_stream()
    with patch('apps.stream.dead_streams_tracker.DeadStreamsTracker'), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[101, 102]), \
         patch('apps.core.api_utils.update_channel_streams', return_value=False) as write:
        result = manager.quarantine_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert 101 in session.quarantined_stream_ids
    manager._save_sessions.assert_called_once()
    write.assert_called_once_with(9, [102], expected_current_stream_ids=[101, 102])


def test_manual_quarantine_succeeds_after_confirmed_removal():
    manager, session, stream = _manager_with_stream()
    with patch('apps.stream.dead_streams_tracker.DeadStreamsTracker'), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[101, 102]), \
         patch('apps.core.api_utils.update_channel_streams', return_value=True) as write:
        result = manager.quarantine_stream(session.session_id, 101)

    assert result is True
    assert stream.status == 'quarantined'
    assert 101 in session.quarantined_stream_ids
    write.assert_called_once()


def test_quarantine_without_remote_write_keeps_cooldown_state():
    manager, session, stream = _manager_with_stream()
    with patch('apps.core.api_utils.update_channel_streams') as write:
        result = manager.quarantine_stream(
            session.session_id, 101, remove_from_dispatcharr=False, reason='looping',
        )

    assert result is True
    assert stream.status == 'quarantined'
    assert stream.status_reason == 'looping'
    assert 101 in session.quarantined_stream_ids
    write.assert_not_called()


def test_failed_revive_restores_quarantine_and_blocklist():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='looping')
    stream.last_status_change = time.time() - 1000
    previous_change = stream.last_status_change
    stream.failure_count = 3
    stream.consecutive_logo_misses = 2
    with patch('apps.core.api_utils.add_streams_to_channel', side_effect=RuntimeError('PATCH failed')), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[]):
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert stream.status_reason == 'looping'
    assert stream.failure_count == 3
    assert stream.consecutive_logo_misses == 2
    assert stream.last_status_change > previous_change
    assert 101 in session.quarantined_stream_ids
    manager._save_sessions.assert_called_once()


def test_successful_revive_moves_to_review_after_confirmed_addition():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='offline')
    with patch('apps.core.api_utils.add_streams_to_channel', return_value=1) as add:
        result = manager.revive_stream(session.session_id, 101)

    assert result is True
    assert stream.status == 'review'
    assert stream.status_reason is None
    assert 101 not in session.quarantined_stream_ids
    add.assert_called_once_with(9, [101], allow_dead_streams=True)
    manager._save_sessions.assert_called_once()


def test_successful_revive_clears_persistent_dead_marker():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='dead')
    db = get_db_manager()
    assert db.mark_stream_dead(stream.url, 101, stream.name, 9, reason='unknown')

    with patch('apps.core.api_utils.add_streams_to_channel', return_value=1):
        result = manager.revive_stream(session.session_id, 101)

    assert result is True
    assert stream.status == 'review'
    assert DeadStreamsTracker().get_dead_stream_reasons([stream.url]) == {}


def test_revive_rolls_back_remote_assignment_when_session_save_fails():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='dead')
    db = get_db_manager()
    assert db.mark_stream_dead(stream.url, 101, stream.name, 9, reason='offline')
    manager._save_sessions.side_effect = [False, True]

    with patch('apps.core.api_utils.add_streams_to_channel', return_value=1), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[101, 102]), \
         patch('apps.core.api_utils.update_channel_streams', return_value=True) as write:
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert stream.status_reason == 'dead'
    assert 101 in session.quarantined_stream_ids
    assert DeadStreamsTracker().get_dead_stream_reasons([stream.url]) == {stream.url: 'offline'}
    write.assert_called_once_with(9, [102], expected_current_stream_ids=[101, 102])
    assert manager._save_sessions.call_args_list == [call(wait=True), call(wait=True)]


def test_failed_dead_marker_clear_does_not_attach_quarantined_stream():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='dead')
    db = get_db_manager()
    assert db.mark_stream_dead(stream.url, 101, stream.name, 9, reason='offline')

    with patch.object(DeadStreamsTracker, 'mark_as_alive', return_value=False), \
         patch('apps.core.api_utils.add_streams_to_channel') as add:
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert 101 in session.quarantined_stream_ids
    assert DeadStreamsTracker().get_dead_stream_reasons([stream.url]) == {stream.url: 'offline'}
    add.assert_not_called()


def test_revive_verifies_dead_marker_is_really_gone():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='dead')
    db = get_db_manager()
    assert db.mark_stream_dead(stream.url, 101, stream.name, 9, reason='unknown')

    with patch.object(DeadStreamsTracker, 'mark_as_alive', return_value=True), \
         patch('apps.core.api_utils.add_streams_to_channel') as add:
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert DeadStreamsTracker().get_dead_stream_reasons([stream.url]) == {stream.url: 'unknown'}
    add.assert_not_called()


def test_failed_revive_restores_dead_marker_and_removes_partial_assignment():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='dead')
    db = get_db_manager()
    assert db.mark_stream_dead(stream.url, 101, stream.name, 9, reason='offline')

    with patch('apps.core.api_utils.add_streams_to_channel', side_effect=RuntimeError('PATCH failed')), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[101, 102]), \
         patch('apps.core.api_utils.update_channel_streams', return_value=True) as write:
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert 101 in session.quarantined_stream_ids
    assert DeadStreamsTracker().get_dead_stream_reasons([stream.url]) == {stream.url: 'offline'}
    write.assert_called_once_with(9, [102], expected_current_stream_ids=[101, 102])


def test_revive_does_not_succeed_when_addition_was_filtered_out():
    manager, session, stream = _manager_with_stream(status='quarantined', reason='offline')
    with patch('apps.core.api_utils.add_streams_to_channel', return_value=0), \
         patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[]):
        result = manager.revive_stream(session.session_id, 101)

    assert result is False
    assert stream.status == 'quarantined'
    assert 101 in session.quarantined_stream_ids


def test_manual_quarantine_api_surfaces_unconfirmed_removal():
    session_manager = Mock()
    session_manager.quarantine_stream.return_value = False
    with Flask(__name__).app_context():
        response, status = quarantine_stream_response(
            session_id='session-9', stream_id=101,
            get_session_manager=lambda: session_manager,
        )

    assert status == 400
    assert 'Dispatcharr removal may be unconfirmed' in response.get_json()['error']


def test_revive_api_surfaces_unconfirmed_assignment():
    session_manager = Mock()
    session_manager.revive_stream.return_value = False
    with Flask(__name__).app_context():
        response, status = revive_stream_response(
            session_id='session-9', stream_id=101,
            get_session_manager=lambda: session_manager,
        )

    assert status == 400
    assert 'Dispatcharr assignment may be unconfirmed' in response.get_json()['error']


def test_fatal_monitor_quarantine_queues_one_remote_removal():
    manager, session, _stream = _manager_with_stream()
    service = object.__new__(StreamMonitoringService)
    service.session_manager = Mock()
    service.session_manager.get_session.return_value = session
    service._state_lock = threading.Lock()
    service.monitors = {}
    service.dead_streams_tracker = Mock()
    service._remove_stream_from_dispatcharr = Mock()
    stats = SimpleNamespace(is_alive=False, error_message='fatal', is_fatal=True)

    service._on_stats_update(session.session_id, 101, stats)

    service.session_manager.quarantine_stream.assert_called_once_with(
        session.session_id, 101, remove_from_dispatcharr=False, reason='fatal'
    )
    service._remove_stream_from_dispatcharr.assert_called_once_with(
        session.session_id, 101, 'dead'
    )
    service.dead_streams_tracker.mark_as_dead.assert_not_called()


def test_auto_removal_checks_current_assignment_and_writes_once():
    _manager, session, _stream = _manager_with_stream(status='quarantined')
    service = object.__new__(StreamMonitoringService)
    service.session_manager = Mock()
    service.session_manager.get_session.return_value = session
    service.dead_streams_tracker = Mock()
    service.io_pool = Mock()
    service.io_pool.submit.side_effect = lambda callback: callback()
    with patch('apps.core.api_utils._fetch_authoritative_channel_stream_ids', return_value=[101, 102]), \
         patch('apps.core.api_utils.update_channel_streams', return_value=True) as write:
        service._remove_stream_from_dispatcharr(session.session_id, 101, 'dead')

    write.assert_called_once_with(9, [102], expected_current_stream_ids=[101, 102])
    service.dead_streams_tracker.mark_as_dead.assert_called_once()

"""Session sync should avoid idle writes while retaining drift and revive behavior."""

import time
from unittest.mock import MagicMock, patch

import pytest

from apps.stream.stream_monitoring_service import (
    EXTERNAL_SYNC_VERIFY_INTERVAL,
    StreamMonitoringService,
)
from apps.stream.stream_session_manager import SessionInfo, StreamInfo


@pytest.fixture
def sync_harness():
    StreamMonitoringService._instance = None
    with patch('stream_monitoring_service.get_session_manager'), \
         patch('stream_monitoring_service.get_screenshot_service'), \
         patch('stream_monitoring_service.get_udi_manager') as get_udi:
        service = StreamMonitoringService()
        udi = MagicMock()
        get_udi.return_value = udi
        service.udi_manager = udi
        stream = StreamInfo(
            stream_id=101, url='http://example.invalid/stream',
            name='Source', channel_id=9, status='stable', reliability_score=90.0,
        )
        session = SessionInfo(
            session_id='session-9', channel_id=9, channel_name='Channel',
            regex_filter='.*', created_at=time.time(), is_active=True,
            streams={101: stream},
        )
        service.session_manager = MagicMock()
        service.session_manager.get_session.return_value = session
        service.session_manager.get_session_owner.return_value = session.session_id
        remote = {'streams': [101]}
        cache = {'streams': [101]}

        def refresh(_channel_id):
            cache['streams'] = list(remote['streams'])
            return True

        udi.refresh_channel_by_id.side_effect = refresh
        udi.get_channel_by_id.side_effect = lambda _channel_id: {'streams': list(cache['streams'])}
        udi.get_playing_stream_ids.return_value = []

        with patch('api_utils.update_channel_streams') as update, \
             patch.object(service.io_pool, 'submit', side_effect=lambda fn, *args: fn(*args)):
            def write(_channel_id, stream_ids, *, expected_current_stream_ids=None):
                if expected_current_stream_ids != remote['streams']:
                    return False
                remote['streams'] = list(stream_ids)
                cache['streams'] = list(stream_ids)
                return True

            update.side_effect = write
            yield service, session, stream, remote, cache, udi, update
    service.io_pool.shutdown(wait=True)
    StreamMonitoringService._instance = None


def test_idle_ticks_do_not_patch_or_fetch_each_second(sync_harness):
    service, session, _stream, _remote, _cache, udi, update = sync_harness
    with patch('stream_monitoring_service.time.time', return_value=100.0):
        assert service._check_sync_enforcement(session)
    for tick in range(101, 160):
        with patch('stream_monitoring_service.time.time', return_value=float(tick)):
            assert service._check_sync_enforcement(session)
    # Sixty one-second ticks: four GETs at t=100/115/130/145, zero PATCHes.
    assert udi.refresh_channel_by_id.call_count == 4
    update.assert_not_called()


def test_external_drift_is_corrected_on_bounded_refresh(sync_harness):
    service, session, _stream, remote, _cache, udi, update = sync_harness
    with patch('stream_monitoring_service.time.time', return_value=100.0):
        service._check_sync_enforcement(session)
    remote['streams'] = [101, 999]
    with patch('stream_monitoring_service.time.time', return_value=101.1):
        service._check_sync_enforcement(session)
    update.assert_not_called()

    with patch('stream_monitoring_service.time.time', return_value=100.0 + EXTERNAL_SYNC_VERIFY_INTERVAL + 0.1):
        service._check_sync_enforcement(session)
    # The monitor worker evaluates before the asynchronous GET completes;
    # the next configured tick applies the freshly observed remote state.
    with patch('stream_monitoring_service.time.time', return_value=100.0 + EXTERNAL_SYNC_VERIFY_INTERVAL + 1.1):
        service._check_sync_enforcement(session)
    update.assert_called_once_with(9, [101], expected_current_stream_ids=[101, 999])
    assert remote['streams'] == [101]
    assert udi.refresh_channel_by_id.call_count == 3  # initial GET, drift GET, post-write GET


def test_authoritative_get_is_submitted_without_blocking_monitor_worker(sync_harness):
    service, session, _stream, _remote, _cache, udi, _update = sync_harness
    with patch.object(service.io_pool, 'submit') as submit, \
         patch('stream_monitoring_service.time.time', return_value=100.0):
        assert service._check_sync_enforcement(session)
    udi.refresh_channel_by_id.assert_not_called()
    submit.assert_called_once()
    callback, callback_session_id, callback_channel_id = submit.call_args.args
    callback(callback_session_id, callback_channel_id)
    udi.refresh_channel_by_id.assert_called_once_with(9)


def test_slow_authoritative_get_does_not_queue_overlapping_requests(sync_harness):
    service, session, _stream, _remote, _cache, udi, update = sync_harness
    with patch.object(service.io_pool, 'submit') as submit:
        with patch('stream_monitoring_service.time.time', return_value=100.0):
            service._check_sync_enforcement(session)
        with patch('stream_monitoring_service.time.time', return_value=116.0):
            service._check_sync_enforcement(session)
    submit.assert_called_once()
    udi.refresh_channel_by_id.assert_not_called()
    update.assert_not_called()


@pytest.mark.parametrize('session_type', ['ffmpeg', 'openstream'])
def test_quarantined_source_is_removed_then_review_can_restore_it(sync_harness, session_type):
    service, session, stream, remote, cache, _udi, update = sync_harness
    session.session_type = session_type
    stream.status = 'quarantined'
    service._update_monitoring_ranks(session.session_id, time.time())
    update.assert_called_once_with(9, [], expected_current_stream_ids=[101])
    assert remote['streams'] == []

    update.reset_mock()
    stream.status = 'review'  # automatic cooldown and manual revive both enter review
    cache['streams'] = []
    service._update_monitoring_ranks(session.session_id, time.time())
    update.assert_called_once_with(9, [101], expected_current_stream_ids=[])
    assert remote['streams'] == [101]

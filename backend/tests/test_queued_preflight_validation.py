import threading
from datetime import datetime, timezone
from unittest.mock import Mock

from apps.stream.queue_execution import StreamCheckQueueExecutionMixin
from apps.stream.teamarr_preflight_service import TeamarrPreflightService


def service_and_metadata():
    service = TeamarrPreflightService.__new__(TeamarrPreflightService)
    service.clock = lambda: 1000
    service.get_config = Mock(return_value={'enabled': True})
    event = {'identity': 'event-100', 'teamarr_id': 100, 'dispatcharr_channel_id': 77,
             'dispatcharr_uuid': 'channel-77', 'event_date': '2026-10-02T20:00:00Z',
             'sync_status': 'in_sync'}
    service._fetch_managed_events = Mock(return_value=[{'id': 100}])
    service._public_event = Mock(return_value=dict(event))
    service._passes_filters = Mock(return_value=True)
    udi = Mock()
    udi.refresh_channel_by_id.return_value = True
    udi.get_channel_by_id.return_value = {'id': 77, 'uuid': 'channel-77', 'streams': [9]}
    service.udi_provider = lambda: udi
    return service, {'event': event, 'trigger_bucket': '20m', 'expires_at': 1100}, udi


def test_expired_preflight_does_not_read_remote_sources():
    service, metadata, udi = service_and_metadata()
    metadata['expires_at'] = 999
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_expired'
    service._fetch_managed_events.assert_not_called()
    udi.refresh_channel_by_id.assert_not_called()


def test_explicit_scan_retains_disabled_automatic_scan_behavior():
    service, metadata, _ = service_and_metadata()
    service.get_config.return_value['enabled'] = False
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_disabled'
    metadata['explicit_scan'] = True
    assert service.validate_queued_check(metadata) is None


def test_channel_reuse_and_rescheduled_source_are_skipped():
    service, metadata, udi = service_and_metadata()
    udi.get_channel_by_id.return_value['uuid'] = 'replacement'
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_channel_changed'
    service._public_event.return_value['identity'] = 'rescheduled'
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_event_changed'


def test_transient_source_failure_is_retryable_and_valid_source_can_execute():
    service, metadata, _ = service_and_metadata()
    assert service.validate_queued_check(metadata) is None
    service._fetch_managed_events.side_effect = TimeoutError()
    result = service.validate_queued_check(metadata)
    assert result['reason'] == 'preflight_source_unavailable' and not result.get('skipped')


def test_expiry_is_rechecked_after_remote_reads():
    service, metadata, udi = service_and_metadata()
    udi.refresh_channel_by_id.side_effect = lambda _: setattr(service, 'clock', lambda: 1200) or True
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_expired'


def test_queued_configuration_and_invalid_time_are_terminal_skips():
    service, metadata, _ = service_and_metadata()
    metadata['config_fingerprint'] = 'obsolete policy'
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_config_changed'
    metadata.pop('config_fingerprint')
    metadata['event']['event_date'] = 'invalid'
    assert service.validate_queued_check(metadata)['reason'] == 'preflight_time_invalid'


def test_source_outage_releases_the_attempt_for_readmission():
    service, metadata, _ = service_and_metadata()
    metadata['attempted_key'] = 'event-100:20m'
    service._clear_attempted = Mock()
    service._record_event = Mock()
    service.record_queued_check_result(metadata, {'success': False, 'reason': 'preflight_source_unavailable'})
    service._clear_attempted.assert_called_once_with('event-100:20m')
    assert service._record_event.call_args.args[0] == 'preflight_deferred'


def test_missing_streams_retry_without_consuming_bucket_until_bounded_exhaustion(monkeypatch):
    service = TeamarrPreflightService.__new__(TeamarrPreflightService)
    service._lock = threading.RLock()
    service._pending_stream_retries = {}
    elapsed = [0]
    service.clock = lambda: 1000 + elapsed[0]
    monkeypatch.setattr('apps.stream.teamarr_preflight_service.time.monotonic', lambda: elapsed[0])
    service._is_attempted = Mock(return_value=False)
    service._active_work_defer_reason = Mock(return_value=None)
    service._channel_has_streams = Mock(return_value=False)
    service._mark_attempted = Mock()
    service._record_event = Mock()
    event = {'identity': 'event', 'dispatcharr_channel_id': 77, 'trigger_bucket': '10m',
             'event_date': datetime.fromtimestamp(1600, tz=timezone.utc).isoformat()}
    config = {'poll_interval_seconds': 30, 'preflight_offset_minutes': 20,
              'retry_offsets_minutes': [10, 3], 'post_start_grace_minutes': 5}
    for attempt in range(4):
        elapsed[0] = attempt * 60
        assert not service._launch_check(event, config)
        service._mark_attempted.assert_not_called()
    elapsed[0] = 240
    assert not service._launch_check(event, config)
    service._mark_attempted.assert_called_once_with(event)
    assert service._pending_stream_retries == {}


def test_queue_validation_skips_probe_but_records_terminal_result(monkeypatch):
    preflight = Mock()
    preflight.validate_queued_check.return_value = {'success': False, 'skipped': True, 'reason': 'preflight_expired'}
    monkeypatch.setattr('apps.stream.teamarr_preflight_service.get_teamarr_preflight_service', lambda: preflight)
    checker = StreamCheckQueueExecutionMixin()
    checker.lock = threading.RLock()
    checker.abort_current_check = threading.Event()
    checker._should_isolate_teamarr_connectivity_abort = Mock(return_value=False)
    checker.check_single_channel = Mock()
    checker._fail_channel_check = Mock(side_effect=lambda cid, error, callback, **kwargs: callback())
    checker._complete_channel_check = Mock()
    metadata = {'source': 'teamarr_preflight'}
    checker._run_specialized_queue_entry_owned({'channel_id': 77, 'queue_entry_token': 3, 'metadata': metadata})
    checker.check_single_channel.assert_not_called()
    assert checker._fail_channel_check.call_args.args[1] == 'preflight_expired'
    preflight.record_queued_check_result.assert_called_once_with(metadata, preflight.validate_queued_check.return_value)
